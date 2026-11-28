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

    # Static schedule context (enriched at registration time from static_trips/stops)
    trip_headsign: str | None = None
    first_stop_name: str | None = None

    # Live tracking state
    vehicle_id: str | None = None
    is_active: bool = False
    is_canceled: bool = False
    has_vehicle_position: bool = False
    last_seen_timestamp: int = 0
    seen_in_current_tick: bool = False
    missed_ticks: int = 0

    # Scheduled departure time from the GTFS-R feed trip descriptor (HH:MM:SS)
    gtfsr_start_time: str | None = None

    # Physical progress along route (from VehiclePositions.current_stop_sequence
    # or the earliest upcoming stop_sequence in TripUpdates)
    max_progress_sequence: int = 0

    # Observed stops keyed by stop_sequence (always holds latest/most accurate delay)
    observed_stops: dict[int, StopObservation] = field(default_factory=dict)

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

    def __init__(
        self,
        ghost_grace_seconds: int = 1200,
        missed_ticks_threshold: int = 5,
    ):
        # Maps trip_instance_id -> ActiveTripTracker
        self.active_trips: dict[str, ActiveTripTracker] = {}
        # Deduplication guard: prevents re-tracking/re-finalizing already completed trips
        self.finalized_ids: set[str] = set()
        self.current_service_date: str | None = None
        self.ghost_grace_seconds = ghost_grace_seconds
        self.missed_ticks_threshold = missed_ticks_threshold

    def begin_tick(self, service_date: str) -> None:
        """Prepare state for a new poll tick."""
        if self.current_service_date and self.current_service_date != service_date:
            # Midnight rollover: prune finalized_ids from previous day
            self.finalized_ids = {
                fid for fid in self.finalized_ids if fid.startswith(f"{service_date}_")
            }
        self.current_service_date = service_date

        for tracker in self.active_trips.values():
            tracker.seen_in_current_tick = False

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
        trip_headsign: str | None = None,
        first_stop_name: str | None = None,
    ) -> ActiveTripTracker | None:
        """Register a known scheduled trip into the in-memory pool."""
        instance_id = f"{service_date}_{trip_id}"
        if instance_id in self.finalized_ids:
            return None
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
                trip_headsign=trip_headsign,
                first_stop_name=first_stop_name,
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
        gtfsr_start_time: str | None = None,
        static_lookup_cb=None,
    ) -> None:
        """Update trip state from GTFS-R TripUpdates feed."""
        instance_id = f"{service_date}_{trip_id}"
        if instance_id in self.finalized_ids:
            return

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
                    trip_headsign=meta.get("trip_headsign"),
                    first_stop_name=meta.get("first_stop_name"),
                )

        if tracker is None:
            return

        tracker.is_active = True
        tracker.seen_in_current_tick = True
        tracker.missed_ticks = 0
        tracker.last_seen_timestamp = max(tracker.last_seen_timestamp, timestamp)
        if vehicle_id:
            tracker.vehicle_id = vehicle_id
        if gtfsr_start_time and not tracker.gtfsr_start_time:
            tracker.gtfsr_start_time = gtfsr_start_time

        # Check explicit cancellation
        if schedule_relationship and schedule_relationship.upper() in ("CANCELED", "CANCELLED", "3"):
            tracker.is_canceled = True
            return

        # Sort incoming stop updates by sequence
        valid_updates = []
        for upd in stop_updates:
            seq = upd.get("stop_sequence")
            s_id = upd.get("stop_id", "")
            delay = upd.get("delay")
            if delay is None:
                delay = upd.get("arrival_delay", upd.get("departure_delay", 0))

            if seq is not None:
                valid_updates.append((int(seq), s_id, int(delay)))

        if not valid_updates:
            return

        valid_updates.sort(key=lambda x: x[0])

        # The smallest stop_sequence in TripUpdates typically represents the vehicle's
        # current/next upcoming stop along the route.
        min_upcoming_seq = valid_updates[0][0]
        if min_upcoming_seq > tracker.max_progress_sequence:
            tracker.max_progress_sequence = min_upcoming_seq

        for seq, s_id, delay in valid_updates:
            tracker.observed_stops[seq] = StopObservation(
                stop_id=s_id,
                stop_sequence=seq,
                delay_seconds=delay,
                observed_at=timestamp,
            )

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
        """Process vehicle GPS position to confirm live physical operation and progress."""
        instance_id = f"{service_date}_{trip_id}"
        if instance_id in self.finalized_ids:
            return

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
                    trip_headsign=meta.get("trip_headsign"),
                    first_stop_name=meta.get("first_stop_name"),
                )

        if tracker:
            tracker.is_active = True
            tracker.has_vehicle_position = True
            tracker.seen_in_current_tick = True
            tracker.missed_ticks = 0
            tracker.vehicle_id = vehicle_id
            tracker.last_seen_timestamp = max(tracker.last_seen_timestamp, timestamp)
            if current_stop_sequence is not None:
                seq = int(current_stop_sequence)
                if seq > tracker.max_progress_sequence:
                    tracker.max_progress_sequence = seq

    def _compute_trip_metrics_and_segments(
        self, tracker: ActiveTripTracker
    ) -> tuple[int | None, int | None, int | None, str | None, list[SegmentDelayRecord]]:
        """Compute start, end, peak delays and segment delays from final observed stops."""
        if not tracker.observed_stops:
            return None, None, None, None, []

        sorted_seqs = sorted(tracker.observed_stops.keys())
        start_delay = tracker.observed_stops[sorted_seqs[0]].delay_seconds
        end_delay = tracker.observed_stops[sorted_seqs[-1]].delay_seconds

        max_delay: int | None = None
        max_delay_stop_id: str | None = None
        for seq in sorted_seqs:
            obs = tracker.observed_stops[seq]
            if max_delay is None or obs.delay_seconds > max_delay:
                max_delay = obs.delay_seconds
                max_delay_stop_id = obs.stop_id

        segments: list[SegmentDelayRecord] = []
        for i in range(len(sorted_seqs) - 1):
            prev_obs = tracker.observed_stops[sorted_seqs[i]]
            curr_obs = tracker.observed_stops[sorted_seqs[i + 1]]
            added = curr_obs.delay_seconds - prev_obs.delay_seconds
            segments.append(
                SegmentDelayRecord(
                    stop_id=curr_obs.stop_id,
                    service_date=tracker.service_date,
                    route_id=tracker.route_id,
                    direction_id=tracker.direction_id,
                    arrival_delay_seconds=curr_obs.delay_seconds,
                    added_delay_seconds=added,
                )
            )

        return start_delay, end_delay, max_delay, max_delay_stop_id, segments

    def evaluate_and_finalize_trips(
        self,
        current_time_seconds: int,
        force_all: bool = False,
        static_lookup_cb=None,
    ) -> tuple[list[FinalizedTrip], list[SegmentDelayRecord], list[tuple[str, str, str, int]]]:
        """Examine in-memory trips, classify completed/expired ones, and evict them.
        
        Trips that are still actively reporting in the live feed are kept in memory
        until they disappear from the feed (or are explicitly canceled).
        
        Returns:
            (finalized_trips, segment_delays, missing_stops_to_record)
        """
        finalized: list[FinalizedTrip] = []
        all_segments: list[SegmentDelayRecord] = []
        missing_stops: list[tuple[str, str, str, int]] = []
        to_evict: list[str] = []

        for instance_id, tracker in self.active_trips.items():
            if not tracker.seen_in_current_tick:
                tracker.missed_ticks += 1

            should_finalize = False
            outcome: TripOutcome | None = None

            # Case 1: Explicitly canceled
            if tracker.is_canceled:
                should_finalize = True
                outcome = TripOutcome.CANCELED

            # Case 2: Ghost Bus
            # Scheduled time window passed + grace period, never appeared in GPS or TripUpdates
            elif (
                not tracker.is_active
                and len(tracker.observed_stops) == 0
                and (
                    current_time_seconds > (tracker.scheduled_end_seconds + self.ghost_grace_seconds)
                    or force_all
                )
            ):
                should_finalize = True
                outcome = TripOutcome.GHOST
                missing_stops.append((
                    tracker.last_stop_id,
                    tracker.service_date,
                    tracker.route_id,
                    tracker.direction_id,
                ))

            # Case 3: Active trip has disappeared from the feed for >= missed_ticks_threshold
            elif tracker.is_active and (
                tracker.missed_ticks >= self.missed_ticks_threshold or force_all
            ):
                # Check if the trip actually had physical operation (GPS/vehicle_id or multi-stop progress)
                has_physical_proof = (
                    tracker.has_vehicle_position
                    or tracker.vehicle_id is not None
                    or tracker.operated_stop_count >= 4
                )

                # If it was only a pre-trip placeholder (no GPS, no vehicle_id, <=3 stops)
                # and its scheduled end hasn't passed yet, keep waiting—it may just be waiting to depart!
                if (
                    not has_physical_proof
                    and not force_all
                    and current_time_seconds <= (tracker.scheduled_end_seconds + self.ghost_grace_seconds)
                ):
                    continue

                should_finalize = True

                if not has_physical_proof:
                    # Never showed physical GPS/vehicle operation and schedule window expired -> GHOST
                    outcome = TripOutcome.GHOST
                    missing_stops.append((
                        tracker.last_stop_id,
                        tracker.service_date,
                        tracker.route_id,
                        tracker.direction_id,
                    ))
                else:
                    # Did the vehicle physically progress near the terminus?
                    # Allow a small buffer (last 15% or 3 stops) since drivers/feeds often
                    # drop updates just before reaching the final terminus stop.
                    terminus_threshold = max(
                        1,
                        min(tracker.total_stops - 3, int(tracker.total_stops * 0.85)),
                    )
                    reached_terminus = (
                        tracker.total_stops > 0
                        and tracker.max_progress_sequence >= terminus_threshold
                    )

                    if reached_terminus:
                        _, end_delay, _, _, _ = self._compute_trip_metrics_and_segments(tracker)
                        delay_val = end_delay or 0
                        outcome = TripOutcome.LATE if delay_val > 300 else TripOutcome.ON_TIME
                    else:
                        outcome = TripOutcome.TERMINATED_EARLY
                        missing_stops.append((
                            tracker.last_stop_id,
                            tracker.service_date,
                            tracker.route_id,
                            tracker.direction_id,
                        ))

            if should_finalize and outcome is not None:
                start_delay, end_delay, max_delay, max_delay_stop_id, segments = (
                    self._compute_trip_metrics_and_segments(tracker)
                )
                if outcome in (TripOutcome.ON_TIME, TripOutcome.LATE, TripOutcome.TERMINATED_EARLY):
                    all_segments.extend(segments)

                first_obs_id = (
                    tracker.observed_stops[min(tracker.observed_stops.keys())].stop_id
                    if tracker.observed_stops else None
                )
                last_obs_id = (
                    tracker.observed_stops[tracker.last_observed_sequence].stop_id
                    if tracker.observed_stops else None
                )
                is_short_turned = outcome == TripOutcome.TERMINATED_EARLY
                op_count = (
                    0
                    if (
                        outcome == TripOutcome.CANCELED
                        and not tracker.has_vehicle_position
                        and tracker.vehicle_id is None
                    )
                    else tracker.operated_stop_count
                )

                finalized_trip = FinalizedTrip(
                    trip_instance_id=tracker.trip_instance_id,
                    trip_id=tracker.trip_id,
                    route_id=tracker.route_id,
                    agency_id=tracker.agency_id,
                    service_date=tracker.service_date,
                    classification=outcome,
                    vehicle_id=tracker.vehicle_id,
                    scheduled_start_time=tracker.gtfsr_start_time,
                    start_delay_seconds=start_delay,
                    end_delay_seconds=end_delay,
                    max_delay_seconds=max_delay,
                    max_delay_stop_id=max_delay_stop_id,
                    scheduled_stop_count=tracker.total_stops,
                    operated_stop_count=op_count,
                    first_operated_stop_id=first_obs_id,
                    last_operated_stop_id=last_obs_id,
                    is_short_turned=is_short_turned,
                    gtfsr_start_time=tracker.gtfsr_start_time,
                    trip_headsign=tracker.trip_headsign,
                    first_stop_name=tracker.first_stop_name,
                )
                finalized.append(finalized_trip)
                to_evict.append(instance_id)

        # Evict finalized trips from memory and record in deduplication set
        for inst_id in to_evict:
            del self.active_trips[inst_id]
            self.finalized_ids.add(inst_id)

        if finalized:
            logger.info(
                "Finalized %d trips (%d still active in memory).",
                len(finalized),
                len(self.active_trips),
            )

        return finalized, all_segments, missing_stops
