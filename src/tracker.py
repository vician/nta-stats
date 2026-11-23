"""In-Memory Trip State Machine for Irish Public Transport Telemetry."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.models import (
    FinalizedTrip,
    SegmentDelayRecord,
    StopObservation,
    TripOutcome,
)

logger = logging.getLogger(__name__)


@dataclass
class ActiveTripTracker:
    """Internal state of a single scheduled or active trip."""

    trip_id: str
    service_date: str  # YYYY-MM-DD
    route_id: str
    agency_id: str
    direction_id: int
    scheduled_start_seconds: int
    scheduled_end_seconds: int
    first_stop_id: str
    last_stop_id: str
    total_stops: int

    # Live tracking state
    vehicle_id: str | None = None
    is_active: bool = False
    is_canceled: bool = False
    last_seen_timestamp: int = 0
    
    # Delay metrics
    start_delay_seconds: int | None = None
    end_delay_seconds: int | None = None
    max_delay_seconds: int | None = None
    max_delay_stop_id: str | None = None

    # Observed stops keyed by stop_sequence
    observed_stops: dict[int, StopObservation] = field(default_factory=dict)
    # Consecutive segment pairs already emitted: set of (prev_seq, curr_seq)
    processed_segments: set[tuple[int, int]] = field(default_factory=set)

    @property
    def trip_instance_id(self) -> str:
        return f"{self.service_date}_{self.trip_id}"

    @property
    def last_observed_sequence(self) -> int:
        return max(self.observed_stops.keys()) if self.observed_stops else 0

    @property
    def operated_stop_count(self) -> int:
        return len(self.observed_stops)


class TripStateManager:
    """Orchestrates in-memory trip tracking, classification, and garbage collection."""

    def __init__(self, ghost_grace_seconds: int = 1200):
        # Maps trip_instance_id -> ActiveTripTracker
        self.active_trips: dict[str, ActiveTripTracker] = {}
        self.ghost_grace_seconds = ghost_grace_seconds

    def register_scheduled_trip(
        self,
        trip_id: str,
        service_date: str,
        route_id: str,
        agency_id: str,
        direction_id: int,
        scheduled_start_seconds: int,
        scheduled_end_seconds: int,
        first_stop_id: str,
        last_stop_id: str,
        total_stops: int,
    ) -> ActiveTripTracker:
        """Register a known scheduled trip into the in-memory pool."""
        instance_id = f"{service_date}_{trip_id}"
        if instance_id not in self.active_trips:
            self.active_trips[instance_id] = ActiveTripTracker(
                trip_id=trip_id,
                service_date=service_date,
                route_id=route_id,
                agency_id=agency_id,
                direction_id=direction_id,
                scheduled_start_seconds=scheduled_start_seconds,
                scheduled_end_seconds=scheduled_end_seconds,
                first_stop_id=first_stop_id,
                last_stop_id=last_stop_id,
                total_stops=total_stops,
            )
        return self.active_trips[instance_id]

    def process_trip_update(
        self,
        trip_id: str,
        service_date: str,
        schedule_relationship: str | None,
        stop_updates: list[dict[str, Any]],
        vehicle_id: str | None = None,
        timestamp: int = 0,
        static_lookup_cb=None,
    ) -> list[SegmentDelayRecord]:
        """Update trip state from GTFS-R TripUpdates feed.
        
        Returns newly detected SegmentDelayRecords for stop-level congestion rollups.
        """
        instance_id = f"{service_date}_{trip_id}"
        tracker = self.active_trips.get(instance_id)

        # If not tracked yet, query static schedule callback if provided
        if tracker is None and static_lookup_cb:
            meta = static_lookup_cb(trip_id)
            if meta:
                tracker = self.register_scheduled_trip(
                    trip_id=trip_id,
                    service_date=service_date,
                    route_id=meta["route_id"],
                    agency_id=meta["agency_id"],
                    direction_id=meta["direction_id"],
                    scheduled_start_seconds=meta["scheduled_start_seconds"],
                    scheduled_end_seconds=meta["scheduled_end_seconds"],
                    first_stop_id=meta["first_stop_id"],
                    last_stop_id=meta["last_stop_id"],
                    total_stops=meta["total_stops"],
                )

        if tracker is None:
            # Trip not part of targeted schedule
            return []

        tracker.is_active = True
        tracker.last_seen_timestamp = max(tracker.last_seen_timestamp, timestamp)
        if vehicle_id:
            tracker.vehicle_id = vehicle_id

        # Check explicit cancellation
        if schedule_relationship and schedule_relationship.upper() in ("CANCELED", "CANCELLED", "3"):
            tracker.is_canceled = True
            return []

        new_segments: list[SegmentDelayRecord] = []

        # Sort incoming stop updates by sequence
        valid_updates = []
        for upd in stop_updates:
            seq = upd.get("stop_sequence")
            s_id = upd.get("stop_id", "")
            delay = upd.get("delay")
            if delay is None:
                # May have arrival/departure delay
                delay = upd.get("arrival_delay", upd.get("departure_delay", 0))

            if seq is not None:
                valid_updates.append((int(seq), s_id, int(delay)))

        valid_updates.sort(key=lambda x: x[0])

        for seq, s_id, delay in valid_updates:
            obs = StopObservation(
                stop_id=s_id,
                stop_sequence=seq,
                delay_seconds=delay,
                observed_at=timestamp,
            )
            tracker.observed_stops[seq] = obs

            # Update start delay if first sequence
            if seq == 1 or tracker.start_delay_seconds is None:
                tracker.start_delay_seconds = delay

            # Update peak delay
            if tracker.max_delay_seconds is None or delay > tracker.max_delay_seconds:
                tracker.max_delay_seconds = delay
                tracker.max_delay_stop_id = s_id

            # End delay is updated to the latest stop sequence observed
            tracker.end_delay_seconds = delay

        # Compute newly completed stop-to-stop segment delays
        sorted_observed_seqs = sorted(tracker.observed_stops.keys())
        for i in range(len(sorted_observed_seqs) - 1):
            prev_seq = sorted_observed_seqs[i]
            curr_seq = sorted_observed_seqs[i + 1]
            seg_key = (prev_seq, curr_seq)

            if seg_key not in tracker.processed_segments:
                tracker.processed_segments.add(seg_key)
                prev_obs = tracker.observed_stops[prev_seq]
                curr_obs = tracker.observed_stops[curr_seq]
                # Added delay is delay at B minus delay at A
                added = curr_obs.delay_seconds - prev_obs.delay_seconds
                new_segments.append(
                    SegmentDelayRecord(
                        stop_id=curr_obs.stop_id,
                        service_date=tracker.service_date,
                        route_id=tracker.route_id,
                        direction_id=tracker.direction_id,
                        arrival_delay_seconds=curr_obs.delay_seconds,
                        added_delay_seconds=added,
                    )
                )

        return new_segments

    def process_vehicle_position(
        self,
        trip_id: str,
        service_date: str,
        vehicle_id: str,
        timestamp: int,
        current_stop_sequence: int | None = None,
        stop_id: str | None = None,
        static_lookup_cb=None,
    ) -> None:
        """Process vehicle GPS position to confirm live physical operation."""
        instance_id = f"{service_date}_{trip_id}"
        tracker = self.active_trips.get(instance_id)

        if tracker is None and static_lookup_cb:
            meta = static_lookup_cb(trip_id)
            if meta:
                tracker = self.register_scheduled_trip(
                    trip_id=trip_id,
                    service_date=service_date,
                    route_id=meta["route_id"],
                    agency_id=meta["agency_id"],
                    direction_id=meta["direction_id"],
                    scheduled_start_seconds=meta["scheduled_start_seconds"],
                    scheduled_end_seconds=meta["scheduled_end_seconds"],
                    first_stop_id=meta["first_stop_id"],
                    last_stop_id=meta["last_stop_id"],
                    total_stops=meta["total_stops"],
                )

        if tracker:
            tracker.is_active = True
            tracker.vehicle_id = vehicle_id
            tracker.last_seen_timestamp = max(tracker.last_seen_timestamp, timestamp)

    def evaluate_and_finalize_trips(
        self,
        current_time_seconds: int,
        force_all: bool = False,
    ) -> tuple[list[FinalizedTrip], list[tuple[str, str, str, int]]]:
        """Examine in-memory trips, classify completed/expired ones, and evict them.
        
        Returns:
            (finalized_trips, missing_stops_to_record)
        """
        finalized: list[FinalizedTrip] = []
        missing_stops: list[tuple[str, str, str, int]] = []
        to_evict: list[str] = []

        for instance_id, tracker in self.active_trips.items():
            should_finalize = False
            outcome: TripOutcome | None = None

            # Case 1: Explicitly canceled
            if tracker.is_canceled:
                should_finalize = True
                outcome = TripOutcome.CANCELED

            # Case 2: Reached scheduled terminus (or final stop sequence)
            elif tracker.last_observed_sequence >= tracker.total_stops and tracker.total_stops > 0:
                should_finalize = True
                end_delay = tracker.end_delay_seconds or 0
                if -60 <= end_delay <= 300:
                    outcome = TripOutcome.ON_TIME
                else:
                    outcome = TripOutcome.LATE

            # Case 3: Ghost Bus
            # Scheduled time window passed + grace period, and never had any GPS ping or observed stops
            elif (
                not tracker.is_active
                and len(tracker.observed_stops) == 0
                and current_time_seconds > (tracker.scheduled_end_seconds + self.ghost_grace_seconds)
            ):
                should_finalize = True
                outcome = TripOutcome.GHOST
                # Mark scheduled terminus/stops as missing
                missing_stops.append((
                    tracker.last_stop_id,
                    tracker.service_date,
                    tracker.route_id,
                    tracker.direction_id,
                ))

            # Case 4: Terminated Early / Short-Turned
            # Vehicle stopped reporting updates, time window passed, but last stop was short of terminus
            elif (
                tracker.is_active
                and tracker.last_observed_sequence < tracker.total_stops
                and (
                    current_time_seconds > (tracker.scheduled_end_seconds + self.ghost_grace_seconds)
                    or force_all
                )
            ):
                should_finalize = True
                outcome = TripOutcome.TERMINATED_EARLY
                # Mark final unreached stop as missing
                missing_stops.append((
                    tracker.last_stop_id,
                    tracker.service_date,
                    tracker.route_id,
                    tracker.direction_id,
                ))

            # Case 5: Force finalize (e.g. shutdown or end-of-day rollover)
            elif force_all:
                should_finalize = True
                if tracker.is_active and len(tracker.observed_stops) > 0:
                    end_delay = tracker.end_delay_seconds or 0
                    if tracker.last_observed_sequence >= tracker.total_stops:
                        outcome = TripOutcome.ON_TIME if -60 <= end_delay <= 300 else TripOutcome.LATE
                    else:
                        outcome = TripOutcome.TERMINATED_EARLY
                else:
                    outcome = TripOutcome.GHOST

            if should_finalize and outcome is not None:
                first_obs_id = (
                    tracker.observed_stops[min(tracker.observed_stops.keys())].stop_id
                    if tracker.observed_stops else None
                )
                last_obs_id = (
                    tracker.observed_stops[tracker.last_observed_sequence].stop_id
                    if tracker.observed_stops else None
                )
                is_short_turned = (
                    outcome == TripOutcome.TERMINATED_EARLY
                    or (tracker.last_observed_sequence < tracker.total_stops and tracker.operated_stop_count > 0)
                )

                finalized_trip = FinalizedTrip(
                    trip_instance_id=tracker.trip_instance_id,
                    trip_id=tracker.trip_id,
                    route_id=tracker.route_id,
                    agency_id=tracker.agency_id,
                    service_date=tracker.service_date,
                    classification=outcome,
                    vehicle_id=tracker.vehicle_id,
                    scheduled_start_time=None,
                    scheduled_end_time=None,
                    actual_start_time=None,
                    actual_end_time=None,
                    start_delay_seconds=tracker.start_delay_seconds,
                    end_delay_seconds=tracker.end_delay_seconds,
                    max_delay_seconds=tracker.max_delay_seconds,
                    max_delay_stop_id=tracker.max_delay_stop_id,
                    scheduled_stop_count=tracker.total_stops,
                    operated_stop_count=tracker.operated_stop_count,
                    first_operated_stop_id=first_obs_id,
                    last_operated_stop_id=last_obs_id,
                    is_short_turned=is_short_turned,
                )
                finalized.append(finalized_trip)
                to_evict.append(instance_id)

        # Evict finalized trips from memory to keep footprint bounded
        for inst_id in to_evict:
            del self.active_trips[inst_id]

        if finalized:
            logger.info("Finalized %d trips (evicted from memory).", len(finalized))

        return finalized, missing_stops
