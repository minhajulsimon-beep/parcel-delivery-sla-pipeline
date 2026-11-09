-- Run these statements in SQLite after loading the sample data.

-- 1. On-time performance by carrier. The denominator is delivered parcels.
SELECT
    carrier,
    COUNT(*) AS all_parcels,
    COUNT(delivered_at) AS delivered_parcels,
    SUM(CASE WHEN on_time = 1 THEN 1 ELSE 0 END) AS on_time_deliveries,
    ROUND(100.0 * SUM(CASE WHEN on_time = 1 THEN 1 ELSE 0 END)
        / NULLIF(COUNT(delivered_at), 0), 1) AS on_time_pct
FROM parcel_lifecycle
GROUP BY carrier
ORDER BY on_time_pct DESC;

-- 2. Late deliveries with the largest delay.
SELECT parcel_id, carrier, region, promised_at, delivered_at, late_hours
FROM parcel_lifecycle
WHERE on_time = 0
ORDER BY late_hours DESC
LIMIT 15;

-- 3. Exception events by region.
SELECT region, COUNT(*) AS parcels_with_exceptions
FROM parcel_lifecycle
WHERE exception_scans > 0
GROUP BY region
ORDER BY parcels_with_exceptions DESC;

-- 4. Open parcels that were already overdue by the demo's reference date.
SELECT parcel_id, carrier, region, promised_at, parcel_status
FROM parcel_lifecycle
WHERE delivered_at IS NULL AND promised_at < '2026-11-05T00:00:00Z'
ORDER BY promised_at ASC;
