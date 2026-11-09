"""CSV ingestion with row validation, idempotency, and an audit trail."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from parcel_sla.synthetic import SCAN_FIELDS, SHIPMENT_FIELDS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VALID_EVENT_TYPES = frozenset((
    "accepted", "sorted", "out_for_delivery", "exception", "delivered"
))


class InvalidRecord(ValueError):
    """A row failed a known data-quality rule."""


def normalized_timestamp(value: str | None) -> str:
    if not value:
        raise InvalidRecord("missing_timestamp")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise InvalidRecord("invalid_timestamp") from exc
    if timestamp.tzinfo is None:
        raise InvalidRecord("timestamp_missing_timezone")
    return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_database(conn: sqlite3.Connection) -> None:
    conn.executescript((PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8"))
    conn.executescript((PROJECT_ROOT / "sql" / "views.sql").read_text(encoding="utf-8"))


def _required(row: dict, field: str) -> str:
    value = row.get(field)
    if value is None or not str(value).strip():
        raise InvalidRecord(f"missing_{field}")
    return str(value).strip()


def _quarantine(conn: sqlite3.Connection, file_name: str, line: int,
                entity: str, reason: str, row: dict) -> None:
    raw = json.dumps({str(k): v for k, v in row.items()}, sort_keys=True)
    conn.execute(
        """INSERT INTO rejected_rows
            (source_file, row_number, entity_type, rejection_reason, raw_record)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(source_file, row_number, entity_type) DO UPDATE SET
                rejection_reason = excluded.rejection_reason,
                raw_record = excluded.raw_record""",
        (file_name, line, entity, reason, raw),
    )


def _load_csv(conn: sqlite3.Connection, path: Path, columns: tuple[str, ...],
              entity: str, handler: Callable[[sqlite3.Connection, dict], str]) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Missing input file: {path}")
    counts = {"source_file": path.name, "input_rows": 0, "inserted_rows": 0,
              "duplicate_rows": 0, "rejected_rows": 0}
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or not set(columns).issubset(reader.fieldnames):
            raise ValueError(f"Unexpected columns in {path.name}. Expected {columns}")
        for row_number, row in enumerate(reader, start=2):
            counts["input_rows"] += 1
            # On a retry, a corrected row should leave quarantine.
            conn.execute(
                "DELETE FROM rejected_rows WHERE source_file = ? AND row_number = ? AND entity_type = ?",
                (path.name, row_number, entity),
            )
            try:
                if None in row:
                    raise InvalidRecord("unexpected_extra_columns")
                outcome = handler(conn, row)
            except InvalidRecord as error:
                counts["rejected_rows"] += 1
                _quarantine(conn, path.name, row_number, entity, str(error), row)
            else:
                counts[f"{outcome}_rows"] += 1
    conn.execute(
        """INSERT INTO ingestion_audit
            (processed_at, source_file, input_rows, inserted_rows, duplicate_rows, rejected_rows)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (utc_now(), path.name, counts["input_rows"], counts["inserted_rows"],
         counts["duplicate_rows"], counts["rejected_rows"]),
    )
    return counts


def _ingest_shipment(conn: sqlite3.Connection, row: dict) -> str:
    parcel_id = _required(row, "parcel_id")
    created = normalized_timestamp(_required(row, "created_at"))
    promised = normalized_timestamp(_required(row, "promised_at"))
    if promised <= created:
        raise InvalidRecord("promise_not_after_creation")
    region = _required(row, "region")
    carrier = _required(row, "carrier")
    service = _required(row, "service_level").lower()
    if service not in {"express", "standard"}:
        raise InvalidRecord("invalid_service_level")
    payload = (parcel_id, created, promised, region, carrier, service)
    existing = conn.execute(
        """SELECT parcel_id, created_at, promised_at, region, carrier, service_level
           FROM shipments WHERE parcel_id = ?""", (parcel_id,)
    ).fetchone()
    if existing:
        if tuple(existing) == payload:
            return "duplicate"
        raise InvalidRecord("conflicting_parcel_id")
    conn.execute(
        """INSERT INTO shipments
           (parcel_id, created_at, promised_at, region, carrier, service_level)
           VALUES (?, ?, ?, ?, ?, ?)""", payload,
    )
    return "inserted"


def _ingest_scan(conn: sqlite3.Connection, row: dict) -> str:
    event_id = _required(row, "event_id")
    parcel_id = _required(row, "parcel_id")
    event_type = _required(row, "event_type")
    if event_type not in VALID_EVENT_TYPES:
        raise InvalidRecord("invalid_event_type")
    event_time = normalized_timestamp(_required(row, "event_time"))
    shipment = conn.execute(
        "SELECT created_at FROM shipments WHERE parcel_id = ?", (parcel_id,)
    ).fetchone()
    if shipment is None:
        raise InvalidRecord("unknown_parcel_id")
    if event_time < shipment["created_at"]:
        raise InvalidRecord("event_before_creation")
    existing = conn.execute(
        "SELECT parcel_id, event_type, event_time FROM scan_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if existing:
        if tuple(existing) == (parcel_id, event_type, event_time):
            return "duplicate"
        raise InvalidRecord("conflicting_event_id")
    conn.execute(
        """INSERT INTO scan_events
           (event_id, parcel_id, event_type, event_time, source_file, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_id, parcel_id, event_type, event_time, row["_source_file"], utc_now()),
    )
    return "inserted"


def ingest(data_dir: Path, db_path: Path,
           batches: tuple[str, ...] = ("scans_batch_01.csv", "scans_batch_02.csv")) -> list[dict]:
    """Load shipments first, then selected scan batches, preserving accepted rows.

    Each file is committed separately. Rerunning a file does not duplicate
    already-loaded parcel or event IDs.
    """
    conn = connect(db_path)
    results: list[dict] = []
    try:
        ensure_database(conn)
        conn.commit()
        with conn:
            results.append(_load_csv(
                conn, data_dir / "shipments.csv", SHIPMENT_FIELDS,
                "shipment", _ingest_shipment,
            ))
        for batch_name in batches:
            if Path(batch_name).name != batch_name:
                raise ValueError("Batch names must be plain filenames")

            def handle_scan(db: sqlite3.Connection, row: dict) -> str:
                return _ingest_scan(db, {**row, "_source_file": batch_name})

            with conn:
                results.append(_load_csv(
                    conn, data_dir / batch_name, SCAN_FIELDS, "scan", handle_scan,
                ))
    finally:
        conn.close()
    return results
