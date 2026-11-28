"""SQLite Database connection, schema setup, and persistence layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from src.models import FinalizedTrip, SegmentDelayRecord, StopDailyStat


def get_db_connection(db_path: Path | str) -> sqlite3.Connection:
    """Create a connection to the SQLite database with WAL and performance PRAGMAs."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(db_path),
        timeout=10.0,
    )
    conn.row_factory = sqlite3.Row

    # Performance and concurrency PRAGMAs
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")

    return conn


def _migrate_db(conn: sqlite3.Connection) -> None:
    """Apply lightweight schema migrations for columns added after initial release."""
    existing_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(trips_finalized);").fetchall()
    }
    with conn:
        if "gtfsr_start_time" not in existing_cols:
            conn.execute("ALTER TABLE trips_finalized ADD COLUMN gtfsr_start_time TEXT;")
        if "trip_headsign" not in existing_cols:
            conn.execute("ALTER TABLE trips_finalized ADD COLUMN trip_headsign TEXT;")
        if "first_stop_name" not in existing_cols:
            conn.execute("ALTER TABLE trips_finalized ADD COLUMN first_stop_name TEXT;")


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize database tables and indices."""
    with conn:
        conn.executescript("""
        -- GTFS Static Lookup Tables
        CREATE TABLE IF NOT EXISTS static_agencies (
            agency_id TEXT PRIMARY KEY,
            agency_name TEXT NOT NULL,
            agency_timezone TEXT DEFAULT 'Europe/Dublin'
        );

        CREATE TABLE IF NOT EXISTS static_routes (
            route_id TEXT PRIMARY KEY,
            agency_id TEXT NOT NULL,
            route_short_name TEXT NOT NULL,
            route_long_name TEXT,
            route_type INTEGER NOT NULL,
            FOREIGN KEY (agency_id) REFERENCES static_agencies(agency_id)
        );

        CREATE TABLE IF NOT EXISTS static_stops (
            stop_id TEXT PRIMARY KEY,
            stop_code TEXT,
            stop_name TEXT NOT NULL,
            stop_lat REAL,
            stop_lon REAL
        );

        CREATE TABLE IF NOT EXISTS static_trips (
            trip_id TEXT PRIMARY KEY,
            route_id TEXT NOT NULL,
            service_id TEXT NOT NULL,
            trip_headsign TEXT,
            direction_id INTEGER,
            scheduled_start_seconds INTEGER NOT NULL,
            scheduled_end_seconds INTEGER NOT NULL,
            first_stop_id TEXT NOT NULL,
            last_stop_id TEXT NOT NULL,
            total_stops INTEGER NOT NULL,
            FOREIGN KEY (route_id) REFERENCES static_routes(route_id)
        );

        CREATE TABLE IF NOT EXISTS static_trip_stop_times (
            trip_id TEXT NOT NULL,
            stop_sequence INTEGER NOT NULL,
            stop_id TEXT NOT NULL,
            arrival_seconds INTEGER NOT NULL,
            departure_seconds INTEGER NOT NULL,
            PRIMARY KEY (trip_id, stop_sequence),
            FOREIGN KEY (trip_id) REFERENCES static_trips(trip_id),
            FOREIGN KEY (stop_id) REFERENCES static_stops(stop_id)
        );
        CREATE INDEX IF NOT EXISTS idx_static_trip_stop_times_stop 
            ON static_trip_stop_times(stop_id);

        -- Telemetry: Finalized Trips
        CREATE TABLE IF NOT EXISTS trips_finalized (
            trip_instance_id TEXT PRIMARY KEY,
            trip_id TEXT NOT NULL,
            route_id TEXT NOT NULL,
            agency_id TEXT NOT NULL,
            service_date TEXT NOT NULL,
            vehicle_id TEXT,
            classification TEXT NOT NULL,
            
            scheduled_start_time TEXT,
            scheduled_end_time TEXT,
            actual_start_time TEXT,
            actual_end_time TEXT,

            start_delay_seconds INTEGER,
            end_delay_seconds INTEGER,
            max_delay_seconds INTEGER,
            max_delay_stop_id TEXT,
            
            scheduled_stop_count INTEGER NOT NULL,
            operated_stop_count INTEGER NOT NULL,
            first_operated_stop_id TEXT,
            last_operated_stop_id TEXT,
            is_short_turned BOOLEAN DEFAULT 0,

            gtfsr_start_time TEXT,
            trip_headsign TEXT,
            first_stop_name TEXT,
            
            finalized_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (route_id) REFERENCES static_routes(route_id)
        );

        CREATE INDEX IF NOT EXISTS idx_trips_finalized_date_route 
            ON trips_finalized(service_date, route_id, classification);
        CREATE INDEX IF NOT EXISTS idx_trips_finalized_agency_date 
            ON trips_finalized(agency_id, service_date);

        -- Telemetry: Daily Stop-Level Bottleneck Rollups
        CREATE TABLE IF NOT EXISTS stop_daily_stats (
            stop_id TEXT NOT NULL,
            service_date TEXT NOT NULL,
            route_id TEXT NOT NULL,
            direction_id INTEGER NOT NULL,
            
            scheduled_arrivals INTEGER DEFAULT 0,
            observed_arrivals INTEGER DEFAULT 0,
            missing_arrivals INTEGER DEFAULT 0,
            
            cumulative_arrival_delay_seconds INTEGER DEFAULT 0,
            max_arrival_delay_seconds INTEGER DEFAULT 0,
            cumulative_added_delay_seconds INTEGER DEFAULT 0,
            
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (stop_id, service_date, route_id, direction_id),
            FOREIGN KEY (stop_id) REFERENCES static_stops(stop_id),
            FOREIGN KEY (route_id) REFERENCES static_routes(route_id)
        );

        CREATE INDEX IF NOT EXISTS idx_stop_daily_stats_date_route 
            ON stop_daily_stats(service_date, route_id);
        """)
    _migrate_db(conn)


def save_finalized_trips(conn: sqlite3.Connection, trips: Iterable[FinalizedTrip]) -> int:
    """Save finalized trips in a single transaction."""
    trip_list = list(trips)
    if not trip_list:
        return 0

    query = """
    INSERT OR REPLACE INTO trips_finalized (
        trip_instance_id, trip_id, route_id, agency_id, service_date,
        vehicle_id, classification,
        scheduled_start_time, scheduled_end_time, actual_start_time, actual_end_time,
        start_delay_seconds, end_delay_seconds, max_delay_seconds, max_delay_stop_id,
        scheduled_stop_count, operated_stop_count,
        first_operated_stop_id, last_operated_stop_id, is_short_turned,
        gtfsr_start_time, trip_headsign, first_stop_name
    ) VALUES (
        :trip_instance_id, :trip_id, :route_id, :agency_id, :service_date,
        :vehicle_id, :classification,
        :scheduled_start_time, :scheduled_end_time, :actual_start_time, :actual_end_time,
        :start_delay_seconds, :end_delay_seconds, :max_delay_seconds, :max_delay_stop_id,
        :scheduled_stop_count, :operated_stop_count,
        :first_operated_stop_id, :last_operated_stop_id, :is_short_turned,
        :gtfsr_start_time, :trip_headsign, :first_stop_name
    );
    """
    params = [
        {
            "trip_instance_id": t.trip_instance_id,
            "trip_id": t.trip_id,
            "route_id": t.route_id,
            "agency_id": t.agency_id,
            "service_date": t.service_date,
            "vehicle_id": t.vehicle_id,
            "classification": t.classification.value,
            "scheduled_start_time": t.scheduled_start_time,
            "scheduled_end_time": t.scheduled_end_time,
            "actual_start_time": t.actual_start_time,
            "actual_end_time": t.actual_end_time,
            "start_delay_seconds": t.start_delay_seconds,
            "end_delay_seconds": t.end_delay_seconds,
            "max_delay_seconds": t.max_delay_seconds,
            "max_delay_stop_id": t.max_delay_stop_id,
            "scheduled_stop_count": t.scheduled_stop_count,
            "operated_stop_count": t.operated_stop_count,
            "first_operated_stop_id": t.first_operated_stop_id,
            "last_operated_stop_id": t.last_operated_stop_id,
            "is_short_turned": 1 if t.is_short_turned else 0,
            "gtfsr_start_time": t.gtfsr_start_time,
            "trip_headsign": t.trip_headsign,
            "first_stop_name": t.first_stop_name,
        }
        for t in trip_list
    ]

    with conn:
        conn.executemany(query, params)
    return len(trip_list)


def record_segment_delays(conn: sqlite3.Connection, records: Iterable[SegmentDelayRecord]) -> int:
    """Accumulate observed arrival delays and segment delays into stop_daily_stats."""
    record_list = list(records)
    if not record_list:
        return 0

    query = """
    INSERT INTO stop_daily_stats (
        stop_id, service_date, route_id, direction_id,
        observed_arrivals, cumulative_arrival_delay_seconds,
        max_arrival_delay_seconds, cumulative_added_delay_seconds
    ) VALUES (
        :stop_id, :service_date, :route_id, :direction_id,
        1, :delay, :delay, :added_delay
    )
    ON CONFLICT(stop_id, service_date, route_id, direction_id) DO UPDATE SET
        observed_arrivals = observed_arrivals + 1,
        cumulative_arrival_delay_seconds = cumulative_arrival_delay_seconds + excluded.cumulative_arrival_delay_seconds,
        max_arrival_delay_seconds = MAX(max_arrival_delay_seconds, excluded.max_arrival_delay_seconds),
        cumulative_added_delay_seconds = cumulative_added_delay_seconds + excluded.cumulative_added_delay_seconds,
        updated_at = CURRENT_TIMESTAMP;
    """
    params = [
        {
            "stop_id": r.stop_id,
            "service_date": r.service_date,
            "route_id": r.route_id,
            "direction_id": r.direction_id,
            "delay": r.arrival_delay_seconds,
            "added_delay": r.added_delay_seconds,
        }
        for r in record_list
    ]

    with conn:
        conn.executemany(query, params)
    return len(record_list)


def increment_scheduled_arrivals(
    conn: sqlite3.Connection,
    stops_plan: Iterable[tuple[str, str, str, int]],
) -> None:
    """Increment scheduled arrival counts for (stop_id, service_date, route_id, direction_id)."""
    stops = list(stops_plan)
    if not stops:
        return

    query = """
    INSERT INTO stop_daily_stats (
        stop_id, service_date, route_id, direction_id,
        scheduled_arrivals
    ) VALUES (?, ?, ?, ?, 1)
    ON CONFLICT(stop_id, service_date, route_id, direction_id) DO UPDATE SET
        scheduled_arrivals = scheduled_arrivals + 1,
        updated_at = CURRENT_TIMESTAMP;
    """
    with conn:
        conn.executemany(query, stops)


def get_finalized_trip_ids(conn: sqlite3.Connection, service_date: str) -> set[str]:
    """Return set of trip_instance_ids already finalized for a given service date."""
    cursor = conn.execute(
        "SELECT trip_instance_id FROM trips_finalized WHERE service_date = ?;",
        (service_date,),
    )
    return {row[0] for row in cursor.fetchall()}


def increment_missing_arrivals(
    conn: sqlite3.Connection,
    stops_plan: Iterable[tuple[str, str, str, int]],
) -> None:
    """Increment missing arrival counts (for ghost or short-turned stops)."""
    stops = list(stops_plan)
    if not stops:
        return

    query = """
    INSERT INTO stop_daily_stats (
        stop_id, service_date, route_id, direction_id,
        missing_arrivals
    ) VALUES (?, ?, ?, ?, 1)
    ON CONFLICT(stop_id, service_date, route_id, direction_id) DO UPDATE SET
        missing_arrivals = missing_arrivals + 1,
        updated_at = CURRENT_TIMESTAMP;
    """
    with conn:
        conn.executemany(query, stops)
