"""Database layer tests verifying PRAGMA settings, schema, and persistence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.database import (
    get_db_connection,
    increment_missing_arrivals,
    increment_scheduled_arrivals,
    init_db,
    record_segment_delays,
    save_finalized_trips,
)
from src.models import FinalizedTrip, SegmentDelayRecord, TripOutcome


def test_sqlite_pragmas(temp_db_path: Path):
    """Verify WAL mode, synchronous=NORMAL, and busy_timeout are enforced."""
    conn = get_db_connection(temp_db_path)
    init_db(conn)

    journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    synchronous = conn.execute("PRAGMA synchronous;").fetchone()[0]
    busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_keys;").fetchone()[0]

    conn.close()

    assert journal_mode.lower() == "wal"
    # synchronous NORMAL is integer 1
    assert synchronous == 1
    assert busy_timeout == 5000
    assert foreign_keys == 1


def test_save_and_query_finalized_trips(db_conn: sqlite3.Connection, seed_static_data):
    """Test inserting finalized trip records and querying them."""
    trip = FinalizedTrip(
        trip_instance_id="2026-11-19_trip_39A_test",
        trip_id="trip_on_time_1",
        route_id="route_39A",
        agency_id="bac",
        service_date="2026-11-19",
        classification=TripOutcome.ON_TIME,
        vehicle_id="BUS_999",
        start_delay_seconds=10,
        end_delay_seconds=120,
        max_delay_seconds=180,
        max_delay_stop_id="stop_3",
        scheduled_stop_count=5,
        operated_stop_count=5,
        first_operated_stop_id="stop_1",
        last_operated_stop_id="stop_5",
        is_short_turned=False,
    )

    count = save_finalized_trips(db_conn, [trip])
    assert count == 1

    row = db_conn.execute(
        "SELECT * FROM trips_finalized WHERE trip_instance_id = ?",
        ("2026-11-19_trip_39A_test",),
    ).fetchone()

    assert row is not None
    assert row["classification"] == "ON_TIME"
    assert row["max_delay_seconds"] == 180
    assert row["end_delay_seconds"] == 120
    assert row["is_short_turned"] == 0


def test_stop_daily_stats_rollups(db_conn: sqlite3.Connection, seed_static_data):
    """Test segment delay and bottleneck accumulation into stop_daily_stats."""
    rec1 = SegmentDelayRecord(
        stop_id="stop_quays_choke",
        service_date="2026-11-19",
        route_id="route_38D",
        direction_id=0,
        arrival_delay_seconds=900,
        added_delay_seconds=450,  # +450s congestion added on this segment
    )
    rec2 = SegmentDelayRecord(
        stop_id="stop_quays_choke",
        service_date="2026-11-19",
        route_id="route_38D",
        direction_id=0,
        arrival_delay_seconds=600,
        added_delay_seconds=300,
    )

    record_segment_delays(db_conn, [rec1, rec2])

    row = db_conn.execute("""
        SELECT observed_arrivals, cumulative_arrival_delay_seconds,
               max_arrival_delay_seconds, cumulative_added_delay_seconds
        FROM stop_daily_stats
        WHERE stop_id = 'stop_quays_choke' AND service_date = '2026-11-19';
    """).fetchone()

    assert row["observed_arrivals"] == 2
    assert row["cumulative_arrival_delay_seconds"] == 1500
    assert row["max_arrival_delay_seconds"] == 900
    assert row["cumulative_added_delay_seconds"] == 750


def test_missing_and_scheduled_arrivals(db_conn: sqlite3.Connection, seed_static_data):
    """Test incrementing scheduled and missing arrivals for ghost or canceled stops."""
    increment_scheduled_arrivals(
        db_conn,
        [("stop_5", "2026-11-19", "route_39A", 0)],
    )
    increment_missing_arrivals(
        db_conn,
        [("stop_5", "2026-11-19", "route_39A", 0)],
    )

    row = db_conn.execute("""
        SELECT scheduled_arrivals, missing_arrivals
        FROM stop_daily_stats
        WHERE stop_id = 'stop_5' AND service_date = '2026-11-19';
    """).fetchone()

    assert row["scheduled_arrivals"] == 1
    assert row["missing_arrivals"] == 1
