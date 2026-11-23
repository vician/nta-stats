"""Domain models and data structures for NTA transit telemetry."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TripOutcome(str, Enum):
    """Punctuality and completion classification for public transit trips."""

    ON_TIME = "ON_TIME"                  # Arrived within -60s to +300s (5 mins) of schedule
    LATE = "LATE"                        # Exceeded +300s delay
    CANCELED = "CANCELED"                # Explicitly marked CANCELED in GTFS-R feed
    GHOST = "GHOST"                      # Scheduled trip that never appeared with GPS/progress
    TERMINATED_EARLY = "TERMINATED_EARLY"# Final operated stop fell short of scheduled terminus


@dataclass
class StopObservation:
    """Realtime stop event observation."""

    stop_id: str
    stop_sequence: int
    delay_seconds: int
    arrival_time: int | None = None
    departure_time: int | None = None
    observed_at: int | None = None


@dataclass
class FinalizedTrip:
    """Represents a completed, canceled, ghost, or cut-short trip for persistence."""

    trip_instance_id: str
    trip_id: str
    route_id: str
    agency_id: str
    service_date: str
    classification: TripOutcome
    vehicle_id: str | None = None
    
    scheduled_start_time: str | None = None
    scheduled_end_time: str | None = None
    actual_start_time: str | None = None
    actual_end_time: str | None = None

    start_delay_seconds: int | None = None
    end_delay_seconds: int | None = None
    max_delay_seconds: int | None = None
    max_delay_stop_id: str | None = None

    scheduled_stop_count: int = 0
    operated_stop_count: int = 0
    first_operated_stop_id: str | None = None
    last_operated_stop_id: str | None = None
    is_short_turned: bool = False


@dataclass
class SegmentDelayRecord:
    """Calculated segment delay between consecutive stops."""

    stop_id: str
    service_date: str
    route_id: str
    direction_id: int
    arrival_delay_seconds: int
    added_delay_seconds: int  # delay_at_stop_B - delay_at_stop_A


@dataclass
class StopDailyStat:
    """Daily aggregated stop performance."""

    stop_id: str
    service_date: str
    route_id: str
    direction_id: int
    scheduled_arrivals: int = 0
    observed_arrivals: int = 0
    missing_arrivals: int = 0
    cumulative_arrival_delay_seconds: int = 0
    max_arrival_delay_seconds: int = 0
    cumulative_added_delay_seconds: int = 0
