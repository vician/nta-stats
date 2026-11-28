"""Main telemetry polling daemon for NTA GTFS-Realtime v2 feeds."""

from __future__ import annotations

import datetime
import logging
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from src.config import Config, get_config
from src.database import (
    get_db_connection,
    get_finalized_trip_ids,
    increment_missing_arrivals,
    init_db,
    record_segment_delays,
    save_finalized_trips,
    _migrate_db,
)
from src.gtfs_static import GTFSStaticManager
from src.tracker import TripStateManager

DUBLIN_TZ = ZoneInfo("Europe/Dublin")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("nta_poller")


class NTAPoller:
    """Polls NTA GTFS-R v2 endpoints, syncs with in-memory tracker, and writes to SQLite."""

    def __init__(
        self,
        config: Config | None = None,
        conn: sqlite3.Connection | None = None,
    ):
        self.config = config or get_config()
        self.conn = conn or get_db_connection(self.config.db_path)
        init_db(self.conn)
        _migrate_db(self.conn)

        self.static_manager = GTFSStaticManager(self.config, self.conn)
        self.tracker = TripStateManager(
            ghost_grace_seconds=self.config.ghost_grace_minutes * 60,
            missed_ticks_threshold=6,
        )
        self.running = False
        self._poll_counter = 0

        self.trip_updates_url = "https://api.nationaltransport.ie/gtfsr/v2/TripUpdates"
        self.vehicles_url = "https://api.nationaltransport.ie/gtfsr/v2/Vehicles"

    def ensure_static_data(self) -> None:
        """Verify static schedule data is present; if not, ingest it."""
        cursor = self.conn.execute("SELECT COUNT(*) FROM static_trips;")
        count = cursor.fetchone()[0]
        if count == 0:
            logger.info("No static GTFS data found. Downloading initial schedule...")
            try:
                self.static_manager.download_and_ingest()
            except Exception as e:
                logger.warning(
                    "Could not download initial static GTFS (%s). Realtime lookups will rely on cached/sample data.",
                    e,
                )

    def _fetch_feed(self, url: str) -> dict[str, Any] | None:
        """Fetch GTFS-R JSON feed from NTA API with authentication."""
        if not self.config.nta_api_key:
            logger.warning("No NTA_API_KEY configured. Skipping live network fetch.")
            return None

        headers = {
            "x-api-key": self.config.nta_api_key,
            "Accept": "application/json",
        }
        params = {"format": "json"}

        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=25.0,
            )
            if resp.status_code == 429:
                logger.warning("NTA API 429 Rate Limit encountered. Backing off.")
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error("Error fetching GTFS-R feed %s: %s", url, e)
            return None

    def poll_once(
        self,
        mock_trip_updates: dict[str, Any] | None = None,
        mock_vehicles: dict[str, Any] | None = None,
        service_date: str | None = None,
        current_time_seconds: int | None = None,
    ) -> dict[str, int]:
        """Execute one polling iteration across TripUpdates and Vehicles feeds.
        
        Supports mock feeds for deterministic testing.
        """
        now = datetime.datetime.now(DUBLIN_TZ)
        if service_date is None:
            service_date = now.strftime("%Y-%m-%d")
        if current_time_seconds is None:
            current_time_seconds = now.hour * 3600 + now.minute * 60 + now.second

        # Ensure deduplication set is seeded if starting/rolling over to a new date
        if self.tracker.current_service_date != service_date:
            existing_ids = get_finalized_trip_ids(self.conn, service_date)
            self.tracker.finalized_ids.update(existing_ids)

        self.tracker.begin_tick(service_date)

        # 1. Fetch Feeds (alternate TripUpdates and Vehicles on live ticks to stay
        # strictly within NTA's 1-request-per-60s Fair Usage Policy across the API key)
        if mock_trip_updates is not None or mock_vehicles is not None:
            tu_data = mock_trip_updates
            veh_data = mock_vehicles
        else:
            if self._poll_counter % 2 == 0:
                tu_data = self._fetch_feed(self.trip_updates_url)
                veh_data = None
            else:
                tu_data = None
                veh_data = self._fetch_feed(self.vehicles_url)
            self._poll_counter += 1

        # 2. Process Vehicles (GPS)
        vehicles_processed = 0
        if veh_data and "entity" in veh_data:
            for entity in veh_data.get("entity", []):
                veh = entity.get("vehicle")
                if not veh:
                    continue
                trip_desc = veh.get("trip", {})
                trip_id = trip_desc.get("trip_id")
                veh_desc = veh.get("vehicle", {})
                vehicle_id = veh_desc.get("id") or veh_desc.get("label")
                ts = int(veh.get("timestamp", time.time()))
                curr_seq = veh.get("current_stop_sequence")
                stop_id = veh.get("stop_id")

                if trip_id and vehicle_id:
                    self.tracker.process_vehicle_position(
                        trip_id=trip_id,
                        service_date=service_date,
                        vehicle_id=vehicle_id,
                        timestamp=ts,
                        current_stop_sequence=curr_seq,
                        stop_id=stop_id,
                        static_lookup_cb=self.static_manager.get_trip_details,
                    )
                    vehicles_processed += 1

        # 3. Process TripUpdates
        trip_updates_processed = 0
        if tu_data and "entity" in tu_data:
            for entity in tu_data.get("entity", []):
                tu = entity.get("trip_update")
                if not tu:
                    continue
                trip_desc = tu.get("trip", {})
                trip_id = trip_desc.get("trip_id")
                if not trip_id:
                    continue

                sched_rel = trip_desc.get("schedule_relationship")
                gtfsr_start_time = trip_desc.get("start_time")  # e.g. "08:45:00"
                veh_info = tu.get("vehicle", {})
                vehicle_id = veh_info.get("id") or veh_info.get("label")
                tu_ts = int(tu.get("timestamp", time.time()))

                # Extract stop updates
                stop_updates = []
                for stu in tu.get("stop_time_update", []):
                    seq = stu.get("stop_sequence")
                    s_id = stu.get("stop_id", "")
                    arr = stu.get("arrival", {})
                    dep = stu.get("departure", {})
                    delay = arr.get("delay") if "delay" in arr else dep.get("delay", 0)

                    stop_updates.append({
                        "stop_sequence": seq,
                        "stop_id": s_id,
                        "delay": delay,
                    })

                self.tracker.process_trip_update(
                    trip_id=trip_id,
                    service_date=service_date,
                    schedule_relationship=sched_rel,
                    stop_updates=stop_updates,
                    vehicle_id=vehicle_id,
                    timestamp=tu_ts,
                    gtfsr_start_time=gtfsr_start_time,
                    static_lookup_cb=self.static_manager.get_trip_details,
                )
                trip_updates_processed += 1

        # 4. Evaluate completions, cancellations, ghosts, and early terminations
        finalized_trips, new_segment_delays, missing_stops = (
            self.tracker.evaluate_and_finalize_trips(
                current_time_seconds=current_time_seconds
            )
        )

        # 5. Persist to SQLite in WAL mode
        saved_trips = save_finalized_trips(self.conn, finalized_trips)
        saved_segments = record_segment_delays(self.conn, new_segment_delays)
        increment_missing_arrivals(self.conn, missing_stops)

        return {
            "vehicles_processed": vehicles_processed,
            "trip_updates_processed": trip_updates_processed,
            "finalized_trips_saved": saved_trips,
            "segment_delays_recorded": saved_segments,
            "missing_stops_recorded": len(missing_stops),
        }

    def start(self) -> None:
        """Start the continuous polling loop."""
        self.running = True
        logger.info(
            "Starting NTA Poller loop (Interval: %ds, DB: %s)...",
            self.config.poll_interval_seconds,
            self.config.db_path,
        )

        # Signal handlers for clean container termination
        def handle_shutdown(signum, frame):
            logger.info("Shutdown signal received (%s). Finalizing remaining trips...", signum)
            self.stop()

        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)

        self.ensure_static_data()

        while self.running:
            tick_start = time.time()
            try:
                stats = self.poll_once()
                logger.info("Poll tick completed: %s", stats)
            except Exception as e:
                logger.error("Unexpected error during poll tick: %s", e, exc_info=True)

            elapsed = time.time() - tick_start
            sleep_time = max(1.0, self.config.poll_interval_seconds - elapsed)
            
            # Sleep in short increments to respond quickly to shutdown signals
            deadline = time.time() + sleep_time
            while self.running and time.time() < deadline:
                time.sleep(1.0)

        logger.info("Poller loop terminated gracefully.")

    def stop(self) -> None:
        """Stop the loop and flush state to database."""
        self.running = False
        try:
            # Final sweep
            now = datetime.datetime.now(DUBLIN_TZ)
            curr_sec = now.hour * 3600 + now.minute * 60 + now.second
            finalized, segments, missing = self.tracker.evaluate_and_finalize_trips(
                current_time_seconds=curr_sec,
                force_all=True,
            )
            save_finalized_trips(self.conn, finalized)
            record_segment_delays(self.conn, segments)
            increment_missing_arrivals(self.conn, missing)
            self.conn.close()
            logger.info("Flushed %d trips on shutdown.", len(finalized))
        except Exception as e:
            logger.error("Error during final flush: %s", e)


def main():
    config = get_config()
    poller = NTAPoller(config)
    poller.start()


if __name__ == "__main__":
    main()
