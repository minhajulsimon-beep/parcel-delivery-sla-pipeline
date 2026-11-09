"""Command line interface for generating, ingesting, and reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from parcel_sla.pipeline import ingest
from parcel_sla.reporting import export_report
from parcel_sla.synthetic import generate

DEFAULT_AS_OF = "2026-11-05T00:00:00Z"
BATCHES = {
    "first": ("scans_batch_01.csv",),
    "second": ("scans_batch_02.csv",),
    "both": ("scans_batch_01.csv", "scans_batch_02.csv"),
}


def _paths(parser: argparse.ArgumentParser, *, include_data: bool = True) -> None:
    if include_data:
        parser.add_argument("--data-dir", type=Path, default=Path("data/sample"),
                            help="Directory with sample shipment and scan CSVs")
    parser.add_argument("--db", type=Path, default=Path("build/parcel_sla.sqlite"),
                        help="Path to the SQLite database")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="parcel-sla", description="Parcel scan ingestion and delivery SLA reporting"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    seed = commands.add_parser("seed", help="Regenerate a reproducible synthetic dataset")
    seed.add_argument("--out", type=Path, default=Path("data/sample"))
    seed.add_argument("--count", type=int, default=800)
    seed.add_argument("--seed", type=int, default=2026)

    ingestion = commands.add_parser("ingest", help="Ingest selected scan batches")
    _paths(ingestion)
    ingestion.add_argument("--batch", choices=tuple(BATCHES), default="both")

    report = commands.add_parser("report", help="Produce HTML, CSV, and JSON exports")
    _paths(report, include_data=False)
    report.add_argument("--out", type=Path, default=Path("build/reports"))
    report.add_argument("--as-of", default=DEFAULT_AS_OF)

    demo = commands.add_parser("demo", help="Ingest both sample batches and export results")
    _paths(demo)
    demo.add_argument("--out", type=Path, default=Path("build/reports"))
    demo.add_argument("--as-of", default=DEFAULT_AS_OF)

    args = parser.parse_args(argv)
    if args.command == "seed":
        print(json.dumps(generate(args.out, count=args.count, seed=args.seed), indent=2))
        return 0
    if args.command in {"ingest", "demo"}:
        batch_names = BATCHES[args.batch] if args.command == "ingest" else BATCHES["both"]
        result = ingest(args.data_dir, args.db, batch_names)
        print("Ingestion summary:")
        print(json.dumps(result, indent=2))
        if args.command == "ingest":
            return 0
    if args.command in {"report", "demo"}:
        report_data = export_report(args.db, args.out, args.as_of)
        print("Delivery KPIs:")
        print(json.dumps(report_data["kpis"], indent=2))
        print(f"Dashboard: {args.out / 'dashboard.html'}")
        return 0
    return 1
