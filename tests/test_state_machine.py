"""Unit and integration tests for in-memory trip state-machine and classifications."""

from __future__ import annotations

import sqlite3

from src.models import TripOutcome
from src.poller import NTAPoller
from src.tracker import TripStateManager
from tests.fixtures.gtfs_realtime_mocks import (
    build_canceled_trip_payload,
    build_late_trip_payload,
    build_on_time_trip_payload,
    build_padding_recovery_trip_payload,
    build_terminated_early_trip_payload,
)


def _simulate_disappearance(poller: NTAPoller, service_date: str, time_sec: int, ticks: int = 2) -> dict[str, int]:
    """Simulate empty feed ticks until missed_ticks_threshold triggers finalization."""
    stats = {}
    for i in range(ticks):
        stats = poller.poll_once(
            mock_trip_updates={"entity": []},
            mock_vehicles={"entity": []},
            service_date=service_date,
            current_time_seconds=time_sec + (i + 1) * 65,
        )
    return stats


def test_on_time_trip_classification(db_conn: sqlite3.Connection, seed_static_data):
    """Scenario 1: Bus stays in memory while active, then finalizes as ON_TIME after disappearing at terminus."""
    tracker = TripStateManager(ghost_grace_seconds=900, missed_ticks_threshold=2)
    tu_feed, veh_feed = build_on_time_trip_payload(trip_id="trip_on_time_1", num_stops=5)

    poller = NTAPoller(conn=db_conn)
    poller.tracker = tracker

    # Tick 1: Active at terminus (stop_sequence=5). Should NOT finalize yet while in feed!
    stats1 = poller.poll_once(
        mock_trip_updates=tu_feed,
        mock_vehicles=veh_feed,
        service_date="2026-11-19",
        current_time_seconds=31500,
    )
    assert stats1["finalized_trips_saved"] == 0
    assert len(tracker.active_trips) == 1

    # Tick 2 & 3: Trip disappears from feed (completed)
    stats_final = _simulate_disappearance(poller, "2026-11-19", 31565, ticks=2)

    assert stats_final["finalized_trips_saved"] == 1
    assert stats_final["segment_delays_recorded"] == 4  # 4 segments between 5 stops
    assert len(tracker.active_trips) == 0

    # Tick 4: If feed echoes the trip again, deduplication guard prevents re-finalizing
    stats_dup = poller.poll_once(
        mock_trip_updates=tu_feed,
        mock_vehicles=veh_feed,
        service_date="2026-11-19",
        current_time_seconds=31750,
    )
    assert stats_dup["finalized_trips_saved"] == 0
    assert len(tracker.active_trips) == 0

    row = db_conn.execute(
        "SELECT * FROM trips_finalized WHERE trip_id = 'trip_on_time_1'"
    ).fetchone()

    assert row is not None
    assert row["classification"] == TripOutcome.ON_TIME.value
    assert row["end_delay_seconds"] == 90
    assert row["operated_stop_count"] == 5
    assert row["scheduled_stop_count"] == 5
    assert row["is_short_turned"] == 0


def test_padding_recovery_and_peak_delay(db_conn: sqlite3.Connection, seed_static_data):
    """Scenario 2: Bus delayed mid-route (+15 mins) but recovers via timetable padding at terminus (+2 mins)."""
    tracker = TripStateManager(ghost_grace_seconds=900, missed_ticks_threshold=2)
    tu_feed, veh_feed = build_padding_recovery_trip_payload(trip_id="trip_padding_1")

    poller = NTAPoller(conn=db_conn)
    poller.tracker = tracker

    poller.poll_once(
        mock_trip_updates=tu_feed,
        mock_vehicles=veh_feed,
        service_date="2026-11-19",
        current_time_seconds=33300,
    )

    stats = _simulate_disappearance(poller, "2026-11-19", 33365, ticks=2)
    assert stats["finalized_trips_saved"] == 1

    row = db_conn.execute(
        "SELECT * FROM trips_finalized WHERE trip_id = 'trip_padding_1'"
    ).fetchone()

    assert row is not None
    assert row["classification"] == TripOutcome.ON_TIME.value
    assert row["max_delay_seconds"] == 900
    assert row["max_delay_stop_id"] == "stop_quays_choke"
    assert row["end_delay_seconds"] == 120

    # Verify added segment delay was recorded once for the bottleneck stop
    bottleneck_row = db_conn.execute(
        "SELECT cumulative_added_delay_seconds FROM stop_daily_stats WHERE stop_id = 'stop_quays_choke'"
    ).fetchone()
    assert bottleneck_row is not None
    assert bottleneck_row["cumulative_added_delay_seconds"] == 450


def test_late_trip_classification(db_conn: sqlite3.Connection, seed_static_data):
    """Scenario 3: Bus exceeds 300s delay at terminus."""
    tracker = TripStateManager(ghost_grace_seconds=900, missed_ticks_threshold=2)
    tu_feed, veh_feed = build_late_trip_payload(trip_id="trip_late_1")

    poller = NTAPoller(conn=db_conn)
    poller.tracker = tracker

    poller.poll_once(
        mock_trip_updates=tu_feed,
        mock_vehicles=veh_feed,
        service_date="2026-11-19",
        current_time_seconds=32850,
    )
    stats = _simulate_disappearance(poller, "2026-11-19", 32915, ticks=2)

    assert stats["finalized_trips_saved"] == 1

    row = db_conn.execute(
        "SELECT * FROM trips_finalized WHERE trip_id = 'trip_late_1'"
    ).fetchone()

    assert row is not None
    assert row["classification"] == TripOutcome.LATE.value
    assert row["end_delay_seconds"] == 450


def test_canceled_trip_classification(db_conn: sqlite3.Connection, seed_static_data):
    """Scenario 4: Trip explicitly marked CANCELED in GTFS-R feed finalizes immediately and deduplicates."""
    tracker = TripStateManager(ghost_grace_seconds=900, missed_ticks_threshold=2)
    tu_feed, veh_feed = build_canceled_trip_payload(trip_id="trip_canceled_1")

    poller = NTAPoller(conn=db_conn)
    poller.tracker = tracker

    stats1 = poller.poll_once(
        mock_trip_updates=tu_feed,
        mock_vehicles=veh_feed,
        service_date="2026-11-19",
        current_time_seconds=30000,
    )
    assert stats1["finalized_trips_saved"] == 1

    # Repeated CANCELED on next tick should not duplicate
    stats2 = poller.poll_once(
        mock_trip_updates=tu_feed,
        mock_vehicles=veh_feed,
        service_date="2026-11-19",
        current_time_seconds=30065,
    )
    assert stats2["finalized_trips_saved"] == 0

    row = db_conn.execute(
        "SELECT * FROM trips_finalized WHERE trip_id = 'trip_canceled_1'"
    ).fetchone()

    assert row is not None
    assert row["classification"] == TripOutcome.CANCELED.value


def test_ghost_trip_classification(db_conn: sqlite3.Connection, seed_static_data):
    """Scenario 5: Scheduled trip that never appeared in GPS feed and expired past grace period."""
    tracker = TripStateManager(ghost_grace_seconds=900, missed_ticks_threshold=2)

    tracker.register_scheduled_trip(
        trip_id="trip_ghost_1",
        service_date="2026-11-19",
        route_id="route_DART",
        agency_id="ir",
        direction_id=0,
        scheduled_start_seconds=25200,
        scheduled_end_seconds=27900,
        first_stop_id="stop_1",
        last_stop_id="stop_5",
        total_stops=5,
    )

    poller = NTAPoller(conn=db_conn)
    poller.tracker = tracker

    stats = poller.poll_once(
        mock_trip_updates={"entity": []},
        mock_vehicles={"entity": []},
        service_date="2026-11-19",
        current_time_seconds=29100,
    )

    assert stats["finalized_trips_saved"] == 1
    assert stats["missing_stops_recorded"] == 1

    row = db_conn.execute(
        "SELECT * FROM trips_finalized WHERE trip_id = 'trip_ghost_1'"
    ).fetchone()

    assert row is not None
    assert row["classification"] == TripOutcome.GHOST.value
    assert row["operated_stop_count"] == 0
    assert row["vehicle_id"] is None

    stat_row = db_conn.execute(
        "SELECT missing_arrivals FROM stop_daily_stats WHERE stop_id = 'stop_5'"
    ).fetchone()
    assert stat_row is not None
    assert stat_row["missing_arrivals"] == 1


def test_terminated_early_trip_classification(db_conn: sqlite3.Connection, seed_static_data):
    """Scenario 6: Trip operated 4 stops of 10, then disappeared from live feed."""
    tracker = TripStateManager(ghost_grace_seconds=900, missed_ticks_threshold=2)
    tu_feed, veh_feed = build_terminated_early_trip_payload(
        trip_id="trip_terminated_early_1",
        vehicle_id="BUS_104",
    )

    poller = NTAPoller(conn=db_conn)
    poller.tracker = tracker

    # 1. Active at stop 4 of 10
    stats1 = poller.poll_once(
        mock_trip_updates=tu_feed,
        mock_vehicles=veh_feed,
        service_date="2026-11-19",
        current_time_seconds=30600,
    )
    assert stats1["finalized_trips_saved"] == 0
    assert len(tracker.active_trips) == 1

    # 2. Disappears for 2 ticks without reaching terminus
    stats2 = _simulate_disappearance(poller, "2026-11-19", 30665, ticks=2)

    assert stats2["finalized_trips_saved"] == 1
    assert stats2["missing_stops_recorded"] == 1
    assert len(tracker.active_trips) == 0

    row = db_conn.execute(
        "SELECT * FROM trips_finalized WHERE trip_id = 'trip_terminated_early_1'"
    ).fetchone()

    assert row is not None
    assert row["classification"] == TripOutcome.TERMINATED_EARLY.value
    assert row["operated_stop_count"] == 4
    assert row["scheduled_stop_count"] == 10
    assert row["is_short_turned"] == 1
