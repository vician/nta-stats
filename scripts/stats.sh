#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-nta-poller}"
DB_PATH_CONTAINER="/app/data/nta_stats.db"
DB_PATH_LOCAL="data/nta_stats.db"

run_sql() {
  local sql="$1"
  if command -v podman >/dev/null 2>&1 && podman ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    podman exec -i "${CONTAINER_NAME}" sqlite3 -header -column "${DB_PATH_CONTAINER}" <<< "$sql"
  elif command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    docker exec -i "${CONTAINER_NAME}" sqlite3 -header -column "${DB_PATH_CONTAINER}" <<< "$sql"
  elif [ -f "${DB_PATH_LOCAL}" ]; then
    sqlite3 -header -column "${DB_PATH_LOCAL}" <<< "$sql"
  else
    echo "Error: Could not find running container '${CONTAINER_NAME}' (podman/docker) or local DB at '${DB_PATH_LOCAL}'." >&2
    exit 1
  fi
}

echo "=================================================================="
echo " 1. OVERALL RELIABILITY BREAKDOWN"
echo "=================================================================="
run_sql "
SELECT 
    classification,
    COUNT(*) AS total_trips,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 1) || '%' AS percentage
FROM trips_finalized
GROUP BY classification
ORDER BY total_trips DESC;
"

echo ""
echo "=================================================================="
echo " 2. WORST-PERFORMING ROUTES LEADERBOARD (min 5 trips)"
echo "=================================================================="
run_sql "
SELECT 
    r.route_short_name AS route,
    COUNT(*) AS total_trips,
    SUM(CASE WHEN t.classification = 'ON_TIME' THEN 1 ELSE 0 END) AS on_time,
    SUM(CASE WHEN t.classification = 'LATE' THEN 1 ELSE 0 END) AS late,
    SUM(CASE WHEN t.classification IN ('CANCELED', 'GHOST') THEN 1 ELSE 0 END) AS ghost_or_canceled,
    SUM(CASE WHEN t.classification = 'TERMINATED_EARLY' THEN 1 ELSE 0 END) AS short_turned,
    ROUND(AVG(t.end_delay_seconds) / 60.0, 1) AS avg_delay_mins
FROM trips_finalized t
JOIN static_routes r ON t.route_id = r.route_id
GROUP BY r.route_short_name
HAVING total_trips >= 5
ORDER BY (SUM(CASE WHEN t.classification = 'ON_TIME' THEN 1.0 ELSE 0 END) / COUNT(*)) ASC
LIMIT 15;
"

echo ""
echo "=================================================================="
echo " 3. CANCELED TRIPS BY ROUTE"
echo "=================================================================="
run_sql "
SELECT 
    r.route_short_name AS route,
    COUNT(*) AS canceled_count
FROM trips_finalized t
JOIN static_routes r ON t.route_id = r.route_id
WHERE t.classification = 'CANCELED'
GROUP BY r.route_short_name
ORDER BY canceled_count DESC;
"

echo ""
echo "=================================================================="
echo " 4. TIMETABLE PADDING EXPOSED (Peak Mid-Route vs. Terminus Delay)"
echo "=================================================================="
run_sql "
SELECT 
    r.route_short_name AS route,
    t.vehicle_id,
    ROUND(t.max_delay_seconds / 60.0, 1) AS peak_delay_mins,
    s.stop_name AS worst_stop,
    ROUND(t.end_delay_seconds / 60.0, 1) AS terminus_delay_mins,
    ROUND((t.max_delay_seconds - t.end_delay_seconds) / 60.0, 1) AS mins_padded_back,
    t.classification
FROM trips_finalized t
JOIN static_routes r ON t.route_id = r.route_id
LEFT JOIN static_stops s ON t.max_delay_stop_id = s.stop_id
WHERE t.max_delay_seconds >= 600
ORDER BY (t.max_delay_seconds - t.end_delay_seconds) DESC
LIMIT 10;
"

echo ""
echo "=================================================================="
echo " 5. TOP 10 BOTTLENECK STOPS (Added Segment Delay)"
echo "=================================================================="
run_sql "
SELECT 
    s.stop_code AS stop_num,
    s.stop_name,
    r.route_short_name AS route,
    d.observed_arrivals AS arrivals,
    ROUND(d.cumulative_added_delay_seconds / 60.0, 1) AS total_added_mins,
    ROUND((d.cumulative_added_delay_seconds * 1.0 / NULLIF(d.observed_arrivals, 0)) / 60.0, 1) AS avg_added_per_bus_mins
FROM stop_daily_stats d
JOIN static_stops s ON d.stop_id = s.stop_id
JOIN static_routes r ON d.route_id = r.route_id
WHERE d.observed_arrivals >= 3
ORDER BY d.cumulative_added_delay_seconds DESC
LIMIT 10;
"

echo ""
echo "=================================================================="
echo " 6. LAST 10 FINALIZED TRIPS"
echo "=================================================================="
run_sql "
SELECT 
    t.finalized_at,
    r.route_short_name AS route,
    t.classification,
    t.vehicle_id,
    ROUND(t.end_delay_seconds / 60.0, 1) AS final_delay_mins,
    ROUND(t.max_delay_seconds / 60.0, 1) AS max_delay_mins,
    t.operated_stop_count || '/' || t.scheduled_stop_count AS stops
FROM trips_finalized t
JOIN static_routes r ON t.route_id = r.route_id
ORDER BY t.finalized_at DESC
LIMIT 10;
"
