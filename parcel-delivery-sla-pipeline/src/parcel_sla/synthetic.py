"""Generate deterministic, fictional parcel operations data for local demos."""

from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHIPMENT_FIELDS = (
    "parcel_id", "created_at", "promised_at", "region", "carrier", "service_level"
)
SCAN_FIELDS = ("event_id", "parcel_id", "event_type", "event_time")
REGIONS = ("North", "Central", "South", "East", "West")
CARRIERS = ("Carrier A", "Carrier B", "Carrier C")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def generate(out_dir: Path, count: int = 800, seed: int = 2026) -> dict[str, int]:
    """Create shipments and two independently loadable scan batches.

    Includes repeated scan IDs and four intentionally invalid rows so that
    data-quality handling is visible in a normal demo run.
    """
    if count < 20:
        raise ValueError("count must be at least 20 to include meaningful test cases")

    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    shipments: list[dict[str, str]] = []
    batch_one: list[dict[str, str]] = []
    batch_two: list[dict[str, str]] = []
    next_event_id = 1

    for number in range(1, count + 1):
        parcel_id = f"P{number:06d}"
        created = base + timedelta(hours=rng.randint(0, 25 * 24))
        service = rng.choices(("standard", "express"), weights=(7, 3))[0]
        promise_hours = rng.randint(45, 85) if service == "standard" else rng.randint(22, 40)
        promised = created + timedelta(hours=promise_hours)
        shipments.append({
            "parcel_id": parcel_id,
            "created_at": _iso(created),
            "promised_at": _iso(promised),
            "region": rng.choice(REGIONS),
            "carrier": rng.choices(CARRIERS, weights=(4, 3, 3))[0],
            "service_level": service,
        })

        def add_event(kind: str, time: datetime, *, to_second_batch: bool = False) -> None:
            nonlocal next_event_id
            record = {
                "event_id": f"E{next_event_id:07d}",
                "parcel_id": parcel_id,
                "event_type": kind,
                "event_time": _iso(time),
            }
            next_event_id += 1
            (batch_two if to_second_batch else batch_one).append(record)

        accepted = created + timedelta(hours=rng.randint(1, 3))
        sorted_at = accepted + timedelta(hours=rng.randint(2, 6))
        out_for_delivery = sorted_at + timedelta(hours=rng.randint(7, 11))
        add_event("accepted", accepted)
        add_event("sorted", sorted_at)

        outcome = rng.random()
        delivered = outcome < 0.76
        exception = 0.76 <= outcome < 0.88
        if delivered or rng.random() < 0.75:
            # Some scans arrive in batch 2 even if their event time is earlier
            # than another scan already loaded from batch 1.
            add_event("out_for_delivery", out_for_delivery, to_second_batch=rng.random() < 0.12)

        if delivered:
            is_late = rng.random() < 0.27
            if is_late:
                delivered_at = promised + timedelta(hours=rng.randint(1, 36))
            else:
                delivered_at = promised - timedelta(hours=rng.randint(2, 15))
            delivered_at = max(delivered_at, out_for_delivery + timedelta(hours=1))
            if rng.random() < 0.08:
                add_event("exception", out_for_delivery + timedelta(minutes=30),
                          to_second_batch=rng.random() < 0.5)
            add_event("delivered", delivered_at, to_second_batch=rng.random() < 0.5)
        elif exception:
            add_event("exception", out_for_delivery + timedelta(hours=2),
                      to_second_batch=rng.random() < 0.5)

    # Eight exact duplicates are deliberately retried by the second source batch.
    batch_two.extend(dict(row) for row in batch_one[:8])
    # Invalid records: unknown parcel, invalid event, invalid time, and event
    # timestamp earlier than its parcel creation.
    first = shipments[0]
    batch_two.extend([
        {"event_id": "BAD0001", "parcel_id": "P999999", "event_type": "accepted",
         "event_time": "2026-10-10T12:00:00Z"},
        {"event_id": "BAD0002", "parcel_id": first["parcel_id"], "event_type": "teleported",
         "event_time": "2026-10-10T12:00:00Z"},
        {"event_id": "BAD0003", "parcel_id": first["parcel_id"], "event_type": "sorted",
         "event_time": "yesterday morning"},
        {"event_id": "BAD0004", "parcel_id": first["parcel_id"], "event_type": "accepted",
         "event_time": "2026-09-01T00:00:00Z"},
    ])
    rng.shuffle(batch_one)
    rng.shuffle(batch_two)

    _write_csv(out_dir / "shipments.csv", SHIPMENT_FIELDS, shipments)
    _write_csv(out_dir / "scans_batch_01.csv", SCAN_FIELDS, batch_one)
    _write_csv(out_dir / "scans_batch_02.csv", SCAN_FIELDS, batch_two)
    return {
        "shipments": len(shipments),
        "batch_01_events": len(batch_one),
        "batch_02_rows": len(batch_two),
    }
