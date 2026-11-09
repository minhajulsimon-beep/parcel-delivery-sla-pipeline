DROP VIEW IF EXISTS parcel_lifecycle;

-- A parcel's latest scan is determined by event time, not the order in
-- which CSV batches were received. A delivered scan is terminal for this demo.
CREATE VIEW parcel_lifecycle AS
WITH ranked_scans AS (
    SELECT
        parcel_id,
        event_type,
        event_time,
        ROW_NUMBER() OVER (
            PARTITION BY parcel_id
            ORDER BY event_time DESC, event_id DESC
        ) AS event_rank
    FROM scan_events
),
first_delivery AS (
    SELECT parcel_id, MIN(event_time) AS delivered_at
    FROM scan_events
    WHERE event_type = 'delivered'
    GROUP BY parcel_id
),
exceptions AS (
    SELECT parcel_id, COUNT(*) AS exception_scans
    FROM scan_events
    WHERE event_type = 'exception'
    GROUP BY parcel_id
)
SELECT
    s.parcel_id,
    s.created_at,
    s.promised_at,
    s.region,
    s.carrier,
    s.service_level,
    latest.event_type AS latest_scan_type,
    latest.event_time AS latest_scan_at,
    d.delivered_at,
    COALESCE(e.exception_scans, 0) AS exception_scans,
    CASE
        WHEN d.delivered_at IS NOT NULL THEN 'delivered'
        WHEN latest.event_type = 'exception' THEN 'exception'
        ELSE 'in_transit'
    END AS parcel_status,
    CASE
        WHEN d.delivered_at IS NULL THEN NULL
        WHEN d.delivered_at <= s.promised_at THEN 1
        ELSE 0
    END AS on_time,
    CASE
        WHEN d.delivered_at IS NULL THEN NULL
        ELSE ROUND((julianday(d.delivered_at) - julianday(s.created_at)) * 24, 2)
    END AS transit_hours,
    CASE
        WHEN d.delivered_at IS NULL THEN NULL
        ELSE ROUND(MAX(0, (julianday(d.delivered_at) - julianday(s.promised_at)) * 24), 2)
    END AS late_hours
FROM shipments s
LEFT JOIN ranked_scans latest ON latest.parcel_id = s.parcel_id AND latest.event_rank = 1
LEFT JOIN first_delivery d ON d.parcel_id = s.parcel_id
LEFT JOIN exceptions e ON e.parcel_id = s.parcel_id;
