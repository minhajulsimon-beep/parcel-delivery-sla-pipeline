"""Integration tests run with Python's standard-library unittest."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from parcel_sla.pipeline import ingest, normalized_timestamp
from parcel_sla.reporting import collect_report, export_report
from parcel_sla.synthetic import generate

AS_OF = "2026-11-05T00:00:00Z"


class ParcelPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = self.root / "raw"
        self.db = self.root / "parcel.sqlite"
        generate(self.raw, count=100, seed=2026)

    def _count(self, table: str) -> int:
        self.assertIn(table, {"scan_events", "shipments", "rejected_rows"})
        with sqlite3.connect(self.db) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_synthetic_data_is_reproducible(self) -> None:
        digest_before = hashlib.sha256((self.raw / "scans_batch_02.csv").read_bytes()).hexdigest()
        generate(self.raw, count=100, seed=2026)
        digest_after = hashlib.sha256((self.raw / "scans_batch_02.csv").read_bytes()).hexdigest()
        self.assertEqual(digest_before, digest_after)

    def test_loads_shipments_scans_and_quarantines_bad_rows(self) -> None:
        runs = ingest(self.raw, self.db)
        self.assertEqual(self._count("shipments"), 100)
        self.assertGreater(self._count("scan_events"), 200)
        self.assertEqual(self._count("rejected_rows"), 4)
        self.assertEqual(runs[-1]["duplicate_rows"], 8)
        self.assertEqual(runs[-1]["rejected_rows"], 4)

    def test_rerun_is_idempotent(self) -> None:
        ingest(self.raw, self.db)
        counts_before = (
            self._count("shipments"), self._count("scan_events"), self._count("rejected_rows")
        )
        first_report = collect_report(self.db, AS_OF)
        ingest(self.raw, self.db)
        counts_after = (
            self._count("shipments"), self._count("scan_events"), self._count("rejected_rows")
        )
        second_report = collect_report(self.db, AS_OF)
        self.assertEqual(counts_before, counts_after)
        self.assertEqual(first_report["kpis"], second_report["kpis"])

    def test_late_arriving_batch_adds_deliveries(self) -> None:
        ingest(self.raw, self.db, batches=("scans_batch_01.csv",))
        first_delivered = collect_report(self.db, AS_OF)["kpis"]["delivered_count"]
        ingest(self.raw, self.db, batches=("scans_batch_02.csv",))
        both_delivered = collect_report(self.db, AS_OF)["kpis"]["delivered_count"]
        self.assertGreater(both_delivered, first_delivered)
        self.assertEqual(self._count("rejected_rows"), 4)

    def test_event_time_not_file_order_determines_latest_scan(self) -> None:
        ingest(self.raw, self.db)
        # A batch-2 out-for-delivery scan was actually recorded before a
        # delivered scan received in batch 1; it must not downgrade status.
        with sqlite3.connect(self.db) as conn:
            cases = conn.execute("""
                SELECT COUNT(*) FROM parcel_lifecycle life
                WHERE life.delivered_at IS NOT NULL
                  AND life.latest_scan_type = 'delivered'
                  AND EXISTS (
                    SELECT 1 FROM scan_events e
                    WHERE e.parcel_id = life.parcel_id
                      AND e.source_file = 'scans_batch_02.csv'
                      AND e.event_type = 'out_for_delivery'
                      AND e.event_time < life.delivered_at
                  )
            """).fetchone()[0]
        self.assertGreater(cases, 0)

    def test_delivered_only_on_time_denominator(self) -> None:
        ingest(self.raw, self.db)
        kpi = collect_report(self.db, AS_OF)["kpis"]
        self.assertEqual(kpi["parcel_count"], 100)
        self.assertLess(kpi["delivered_count"], kpi["parcel_count"])
        self.assertLessEqual(kpi["on_time_count"], kpi["delivered_count"])
        self.assertAlmostEqual(
            kpi["on_time_pct"],
            round(kpi["on_time_count"] / kpi["delivered_count"] * 100, 1),
        )

    def test_report_exports_are_consistent(self) -> None:
        ingest(self.raw, self.db)
        report_dir = self.root / "results"
        report = export_report(self.db, report_dir, AS_OF)
        self.assertEqual(json.loads((report_dir / "kpis.json").read_text()), report["kpis"])
        self.assertIn("Synthetic sample data", (report_dir / "dashboard.html").read_text())
        with (report_dir / "parcel_lifecycle.csv").open(newline="") as file:
            self.assertEqual(len(list(csv.DictReader(file))), 100)
        self.assertTrue((report_dir / "carrier_performance.csv").exists())

    def test_repaired_row_leaves_quarantine_after_retry(self) -> None:
        ingest(self.raw, self.db)
        batch = self.raw / "scans_batch_02.csv"
        with batch.open(newline="") as file:
            reader = csv.DictReader(file)
            fields = reader.fieldnames
            rows = list(reader)
        with (self.raw / "shipments.csv").open(newline="") as file:
            created = next(csv.DictReader(file))["created_at"]
        for record in rows:
            if record["event_id"] == "BAD0003":
                record["event_time"] = created
                break
        else:
            self.fail("Expected deliberately invalid row was not generated")
        with batch.open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        ingest(self.raw, self.db, batches=("scans_batch_02.csv",))
        self.assertEqual(self._count("rejected_rows"), 3)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM scan_events WHERE event_id='BAD0003'").fetchone()[0], 1
            )

    def test_conflicting_event_id_goes_to_quarantine(self) -> None:
        ingest(self.raw, self.db)
        original_count = self._count("scan_events")
        batch = self.raw / "scans_batch_02.csv"
        with batch.open(newline="") as file:
            reader = csv.DictReader(file)
            fields = reader.fieldnames
            rows = list(reader)
        # One of the eight repeated source events is changed on the next run.
        with (self.raw / "scans_batch_01.csv").open(newline="") as file:
            first_batch_ids = {row["event_id"] for row in csv.DictReader(file)}
        changed = False
        for record in rows:
            if record["event_id"] in first_batch_ids:
                record["event_type"] = "delivered" if record["event_type"] != "delivered" else "sorted"
                changed = True
                break
        self.assertTrue(changed)
        with batch.open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        ingest(self.raw, self.db, batches=("scans_batch_02.csv",))
        self.assertEqual(self._count("scan_events"), original_count)
        self.assertEqual(self._count("rejected_rows"), 5)

    def test_wrong_source_schema_stops_its_batch(self) -> None:
        batch = self.raw / "scans_batch_01.csv"
        batch.write_text("event_id,parcel_id,bad_column\nE123,P000001,accepted\n")
        with self.assertRaisesRegex(ValueError, "Unexpected columns"):
            ingest(self.raw, self.db, batches=("scans_batch_01.csv",))
        self.assertEqual(self._count("shipments"), 100)
        self.assertEqual(self._count("scan_events"), 0)

    def test_timestamp_validation_rejects_naive_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "timestamp_missing_timezone"):
            normalized_timestamp("2026-10-20T12:00:00")
        self.assertEqual(
            normalized_timestamp("2026-10-20T14:00:00+02:00"), "2026-10-20T12:00:00Z"
        )


if __name__ == "__main__":
    unittest.main()
