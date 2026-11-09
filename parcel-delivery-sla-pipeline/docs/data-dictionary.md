# Data dictionary

All timestamps use ISO 8601 with UTC timezone (`Z`). Input timestamps with an explicit offset are normalized to UTC on ingestion.

## `shipments.csv`

| Column | Meaning | Validation |
| --- | --- | --- |
| `parcel_id` | Stable parcel identifier | Required; unique |
| `created_at` | Parcel registration time | Valid timezone-aware timestamp |
| `promised_at` | Deadline committed to the customer | Must be later than creation |
| `region` | Fictional delivery region | Required |
| `carrier` | Fictional carrier label | Required |
| `service_level` | `standard` or `express` | Controlled vocabulary |

## Scan files

| Column | Meaning | Validation |
| --- | --- | --- |
| `event_id` | Stable scan identifier | Required; unique across batches |
| `parcel_id` | Related parcel | Must exist in shipments |
| `event_type` | `accepted`, `sorted`, `out_for_delivery`, `exception`, or `delivered` | Controlled vocabulary |
| `event_time` | When the scan occurred, not when ingested | Valid UTC-normalizable timestamp; not before creation |

## Core metrics

| KPI | Exact definition |
| --- | --- |
| On-time rate | Delivered parcels with `delivered_at <= promised_at` / all delivered parcels |
| Completion rate | Delivered parcels / all parcels |
| Late deliveries | Delivered parcels with `delivered_at > promised_at` |
| Open overdue | Parcels with no delivery scan and `promised_at < as_of` |
| Average transit time | Average hours from creation to delivery for delivered parcels |
| Exception history | Parcels with one or more exception scans, including eventually delivered |

The event-level table is preserved for audits. `parcel_lifecycle` is a SQL view and can be regenerated from the source records; there is no extra manually maintained status table.
