"""Operational metrics, CSV exports, and a portable HTML report."""

from __future__ import annotations

import csv
import html
import json
import sqlite3
from pathlib import Path

from parcel_sla.pipeline import connect, normalized_timestamp


SUMMARY_SQL = """
SELECT
    COUNT(*) AS parcel_count,
    COUNT(delivered_at) AS delivered_count,
    SUM(CASE WHEN on_time = 1 THEN 1 ELSE 0 END) AS on_time_count,
    SUM(CASE WHEN on_time = 0 THEN 1 ELSE 0 END) AS late_count,
    SUM(CASE WHEN delivered_at IS NULL AND promised_at < ? THEN 1 ELSE 0 END) AS open_overdue_count,
    SUM(CASE WHEN exception_scans > 0 THEN 1 ELSE 0 END) AS parcels_with_exceptions,
    ROUND(AVG(transit_hours), 2) AS average_transit_hours
FROM parcel_lifecycle
"""

BREAKDOWN_SQL = """
SELECT {dimension},
    COUNT(*) AS parcel_count,
    COUNT(delivered_at) AS delivered_count,
    SUM(CASE WHEN on_time = 1 THEN 1 ELSE 0 END) AS on_time_count,
    SUM(CASE WHEN on_time = 0 THEN 1 ELSE 0 END) AS late_count,
    SUM(CASE WHEN delivered_at IS NULL AND promised_at < ? THEN 1 ELSE 0 END) AS open_overdue_count,
    ROUND(AVG(transit_hours), 2) AS average_transit_hours
FROM parcel_lifecycle
GROUP BY {dimension}
ORDER BY parcel_count DESC, {dimension} ASC
"""

PARCEL_SQL = """
SELECT parcel_id, created_at, promised_at, region, carrier, service_level,
       latest_scan_type, latest_scan_at, delivered_at, parcel_status,
       on_time, transit_hours, late_hours, exception_scans
FROM parcel_lifecycle
ORDER BY parcel_id
"""


def pct(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def _breakdown(conn: sqlite3.Connection, dimension: str, as_of: str) -> list[dict]:
    if dimension not in {"region", "carrier"}:
        raise ValueError("Unsupported breakdown dimension")
    records = [dict(row) for row in conn.execute(
        BREAKDOWN_SQL.format(dimension=dimension), (as_of,),
    )]
    for row in records:
        row["on_time_count"] = row["on_time_count"] or 0
        row["late_count"] = row["late_count"] or 0
        row["open_overdue_count"] = row["open_overdue_count"] or 0
        row["on_time_pct"] = pct(row["on_time_count"], row["delivered_count"])
    return records


def collect_report(db_path: Path, as_of: str) -> dict:
    """Return metrics with an explicit reference date for overdue parcels."""
    as_of = normalized_timestamp(as_of)
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}. Run 'parcel-sla run' first.")
    conn = connect(db_path)
    try:
        row = conn.execute(SUMMARY_SQL, (as_of,)).fetchone()
        metrics = dict(row)
        for name in ("on_time_count", "late_count", "open_overdue_count", "parcels_with_exceptions"):
            metrics[name] = metrics[name] or 0
        metrics["on_time_pct"] = pct(metrics["on_time_count"], metrics["delivered_count"])
        metrics["delivery_completion_pct"] = pct(metrics["delivered_count"], metrics["parcel_count"])
        metrics["as_of"] = as_of
        regions = _breakdown(conn, "region", as_of)
        carriers = _breakdown(conn, "carrier", as_of)
        rejections = [dict(row) for row in conn.execute(
            """SELECT rejection_reason, COUNT(*) AS occurrences
               FROM rejected_rows GROUP BY rejection_reason
               ORDER BY occurrences DESC, rejection_reason"""
        )]
        quality = {
            "valid_scan_events": conn.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0],
            "rejected_row_count": sum(r["occurrences"] for r in rejections),
            "rejected_by_reason": rejections,
            "parcels_without_scans": conn.execute(
                """SELECT COUNT(*) FROM shipments s WHERE NOT EXISTS
                   (SELECT 1 FROM scan_events e WHERE e.parcel_id = s.parcel_id)"""
            ).fetchone()[0],
        }
        parcels = [dict(row) for row in conn.execute(PARCEL_SQL)]
        return {"kpis": metrics, "regions": regions, "carriers": carriers,
                "data_quality": quality, "parcels": parcels}
    finally:
        conn.close()


def _csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _format(value: float | int | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:,}{suffix}"


def _html_table(rows: list[dict], name: str) -> str:
    if not rows:
        return "<p>No data available.</p>"
    chunks: list[str] = []
    for row in rows:
        value = row["on_time_pct"]
        bar = max(0.0, min(100.0, value or 0.0))
        label = html.escape(str(row[name]))
        chunks.append(
            f"<tr><td class='entity'>{label}</td>"
            f"<td class='number'>{row['parcel_count']:,}</td>"
            f"<td class='number'>{row['delivered_count']:,}</td>"
            "<td class='performance'>"
            f"<span class='bar-bg'><span class='bar-fill' style='width:{bar:.1f}%'></span></span>"
            f"<span class='rate'>{_format(value, '%')}</span>"
            "</td>"
            f"<td class='number'>{row['open_overdue_count']:,}</td></tr>"
        )
    return (
        "<table><thead><tr><th>" + name.title() + "</th><th>Parcels</th>"
        "<th>Delivered</th><th>On-time rate</th><th>Open overdue</th>"
        "</tr></thead><tbody>" + "".join(chunks) + "</tbody></table>"
    )


def _html_dashboard(report: dict) -> str:
    k = report["kpis"]
    q = report["data_quality"]
    cards = [
        ("Total parcels", _format(k["parcel_count"]), "Shipment records"),
        ("Delivered", _format(k["delivered_count"]),
         f"{_format(k['delivery_completion_pct'], '%')} of all parcels"),
        ("On-time delivery", _format(k["on_time_pct"], "%"), "Of delivered parcels only"),
        ("Open & overdue", _format(k["open_overdue_count"]), "Not delivered by reference date"),
        ("Late deliveries", _format(k["late_count"]), "Delivered after the promised time"),
        ("Exception history", _format(k["parcels_with_exceptions"]),
         "Parcels with at least one exception scan"),
    ]
    card_html = "".join(
        "<div class='metric'><div class='metric-label'>" + html.escape(label) + "</div>"
        "<div class='metric-value'>" + html.escape(value) + "</div>"
        "<div class='metric-caption'>" + html.escape(detail) + "</div></div>"
        for label, value, detail in cards
    )
    issues = "".join(
        f"<li><code>{html.escape(item['rejection_reason'])}</code>"
        f"<span>{item['occurrences']}</span></li>"
        for item in q["rejected_by_reason"]
    )
    if not issues:
        issues = "<li>None</li>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Parcel Delivery | SLA Control Panel</title>
<style>
:root {{ color-scheme: light; font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #f4f7fa; color: #13283d; }}
header {{ background: #112940; color: #fff; padding: 48px max(24px, calc((100vw - 1120px) / 2)); }}
header .eyebrow {{ color: #73c6d6; font-size: 12px; font-weight: 750; letter-spacing: 2px; text-transform: uppercase; }}
header h1 {{ font-size: clamp(28px, 4vw, 39px); margin: 10px 0 8px; letter-spacing: -1.5px; }}
header p {{ margin: 0; color: #bbcad7; font-size: 14px; line-height: 1.7; }}
main {{ max-width: 1168px; margin: auto; padding: 26px 24px 64px; }}
.metrics {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-bottom: 25px; }}
.metric {{ background: #fff; padding: 24px; border: 1px solid #e4eaf0; border-radius: 12px; box-shadow: 0 2px 8px #102a4010; }}
.metric-label {{ font-size: 13px; color: #586f80; font-weight: 650; }}
.metric-value {{ font-size: 32px; font-weight: 760; margin: 10px 0 9px; letter-spacing: -1px; }}
.metric-caption {{ font-size: 12px; color: #748899; line-height: 1.4; }}
.panel {{ background: #fff; border: 1px solid #e4eaf0; border-radius: 12px; padding: 25px; margin-bottom: 18px; overflow: auto; }}
h2 {{ font-size: 17px; letter-spacing: -0.25px; margin: 0 0 8px; }}
.panel-sub {{ color: #6b8091; font-size: 13px; margin: 0 0 16px; line-height: 1.6; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; min-width: 560px; }}
th {{ text-align: left; padding: 11px 8px; color: #688092; border-bottom: 1px solid #e9eef2; font-weight: 650; }}
td {{ padding: 14px 8px; border-bottom: 1px solid #edf1f4; }}
tr:last-child td {{ border-bottom: 0; }}
.number {{ text-align: left; font-variant-numeric: tabular-nums; }}
.entity {{ font-weight: 650; }}
.performance {{ min-width: 175px; white-space: nowrap; }}
.bar-bg {{ display: inline-block; height: 8px; width: 92px; background: #e8eff3; border-radius: 10px; margin-right: 9px; vertical-align: middle; overflow: hidden; }}
.bar-fill {{ display: block; height: 100%; background: #2697a9; border-radius: 10px; }}
.rate {{ font-size: 12px; color: #486476; font-variant-numeric: tabular-nums; }}
.columns {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
ul.issues {{ list-style: none; padding: 0; margin: 14px 0 0; font-size: 13px; }}
.issues li {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #edf1f4; }}
.issues li:last-child {{ border: 0; }}
code {{ color: #286678; background: #ecf5f7; padding: 2px 6px; border-radius: 4px; }}
.note {{ font-size: 12px; color: #6b8091; line-height: 1.7; }}
footer {{ color: #6b8091; font-size: 12px; margin-top: 22px; line-height: 1.7; }}
@media (max-width: 780px) {{ .metrics {{ grid-template-columns: repeat(2, 1fr); }} .columns {{ grid-template-columns: 1fr; }} }}
@media (max-width: 480px) {{ .metrics {{ grid-template-columns: 1fr; }} header {{ padding-top: 34px; padding-bottom: 34px; }} }}
</style>
</head>
<body>
<header>
  <div class="eyebrow">Operational analytics / Synthetic sample data</div>
  <h1>Parcel delivery SLA control panel</h1>
  <p>Shipment-level service performance reconstructed from operational scan events.<br>
  Reference date: {html.escape(k['as_of'])} UTC</p>
</header>
<main>
  <section class="metrics">{card_html}</section>
  <section class="panel"><h2>Regional performance</h2>
    <p class="panel-sub">Delivery punctuality for each region, based on the promised timestamp.</p>
    {_html_table(report['regions'], 'region')}
  </section>
  <section class="panel"><h2>Carrier comparison</h2>
    <p class="panel-sub">Compare service levels without treating undelivered parcels as on-time.</p>
    {_html_table(report['carriers'], 'carrier')}
  </section>
  <section class="columns">
    <div class="panel"><h2>Data quality</h2>
      <p class="panel-sub">Rejected source records are quarantined rather than loaded silently.</p>
      <div class="note">Accepted scan events: <strong>{q['valid_scan_events']:,}</strong><br>
      Rejected source rows: <strong>{q['rejected_row_count']:,}</strong><br>
      Parcels without any scan: <strong>{q['parcels_without_scans']:,}</strong></div>
      <ul class="issues">{issues}</ul>
    </div>
    <div class="panel"><h2>Metric definitions</h2>
      <p class="note"><strong>On-time delivery:</strong> Delivered at or before the promised UTC timestamp.
      The denominator includes delivered parcels only.</p>
      <p class="note"><strong>Open overdue:</strong> Not delivered and the promise timestamp is earlier than
      the report reference date.</p>
      <p class="note"><strong>Exception history:</strong> Any exception scan, even if delivery later succeeded.</p>
      <p class="note"><strong>Average delivery time:</strong> {_format(k['average_transit_hours'], ' hours')}
      among delivered parcels.</p>
    </div>
  </section>
  <footer>This is a reproducible demonstration with generated data, not live carrier performance.
  Source files and SQL logic are available in the repository.</footer>
</main>
</body>
</html>"""


def export_report(db_path: Path, out_dir: Path, as_of: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = collect_report(db_path, as_of)
    (out_dir / "kpis.json").write_text(
        json.dumps(report["kpis"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "data_quality.json").write_text(
        json.dumps(report["data_quality"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _csv(out_dir / "region_performance.csv", report["regions"])
    _csv(out_dir / "carrier_performance.csv", report["carriers"])
    _csv(out_dir / "parcel_lifecycle.csv", report["parcels"])
    (out_dir / "dashboard.html").write_text(_html_dashboard(report), encoding="utf-8")
    return report
