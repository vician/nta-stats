"""GTFS Static schedule ingestion, agency filtering, and SQLite caching."""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import requests

from src.config import Config

logger = logging.getLogger(__name__)


def time_str_to_seconds(time_str: str) -> int:
    """Convert GTFS time format HH:MM:SS (which can exceed 24:00:00) to seconds from midnight."""
    parts = time_str.strip().split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid GTFS time string: {time_str}")
    h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    return h * 3600 + m * 60 + s


def seconds_to_time_str(seconds: int) -> str:
    """Convert seconds from midnight back to HH:MM:SS."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


class GTFSStaticManager:
    """Manages static GTFS schedule downloading, extraction, filtering, and caching."""

    def __init__(self, config: Config, conn: sqlite3.Connection):
        self.config = config
        self.conn = conn

    def download_and_ingest(
        self,
        zip_source: str | Path | None = None,
        store_stop_times: bool = False,
    ) -> dict[str, int]:
        """Download or read GTFS zip, parse target agencies, and populate SQLite tables.
        
        Args:
            zip_source: Local file path or URL. Defaults to config.gtfs_static_url.
            store_stop_times: If True, also store all stop_times rows (millions of rows).
            
        Returns:
            Dictionary with counts of ingested entities.
        """
        if zip_source is None:
            zip_source = self.config.gtfs_static_url

        if isinstance(zip_source, str) and zip_source.startswith("http"):
            logger.info("Downloading GTFS static archive from %s...", zip_source)
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=True) as tmp_file:
                with requests.get(zip_source, stream=True, timeout=120) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            tmp_file.write(chunk)
                tmp_file.flush()
                with zipfile.ZipFile(tmp_file.name) as archive:
                    return self._parse_and_insert(archive, store_stop_times=store_stop_times)

        with zipfile.ZipFile(zip_source) as archive:
            return self._parse_and_insert(archive, store_stop_times=store_stop_times)

    def _parse_and_insert(
        self,
        archive: zipfile.ZipFile,
        store_stop_times: bool = False,
    ) -> dict[str, int]:
        """Parse GTFS zip file streams and populate SQLite in a single transaction."""
        namelist = set(archive.namelist())
        for required in ("agency.txt", "routes.txt", "trips.txt", "stops.txt", "stop_times.txt"):
            if required not in namelist:
                raise ValueError(f"Missing required GTFS file: {required}")

        counts = {
            "agencies": 0,
            "routes": 0,
            "stops": 0,
            "trips": 0,
            "stop_times": 0,
        }

        # 1. Parse Agencies and identify target agencies
        target_agency_ids: set[str] = set()
        agency_rows = []
        with archive.open("agency.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                a_id = row.get("agency_id", "").strip()
                a_name = row.get("agency_name", "").strip()
                if self.config.matches_target_agency(a_id, a_name):
                    target_agency_ids.add(a_id)
                    agency_rows.append((
                        a_id,
                        a_name,
                        row.get("agency_timezone", "Europe/Dublin"),
                    ))

        logger.info("Found %d target agencies: %s", len(target_agency_ids), target_agency_ids)

        # 2. Parse Routes for target agencies
        target_route_ids: set[str] = set()
        route_rows = []
        with archive.open("routes.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                a_id = row.get("agency_id", "").strip()
                r_id = row.get("route_id", "").strip()
                # If agency_id in routes is empty and only one agency exists, or matches target
                if (not a_id and len(target_agency_ids) == 1) or a_id in target_agency_ids:
                    target_route_ids.add(r_id)
                    route_rows.append((
                        r_id,
                        a_id,
                        row.get("route_short_name", "").strip(),
                        row.get("route_long_name", "").strip(),
                        int(row.get("route_type", "3")),
                    ))

        logger.info("Found %d routes for target agencies", len(target_route_ids))

        # 3. Parse Trips for target routes
        target_trip_ids: set[str] = set()
        trip_pre_rows: dict[str, dict[str, Any]] = {}
        with archive.open("trips.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                r_id = row.get("route_id", "").strip()
                t_id = row.get("trip_id", "").strip()
                if r_id in target_route_ids:
                    target_trip_ids.add(t_id)
                    direction_str = row.get("direction_id", "").strip()
                    direction_id = int(direction_str) if direction_str else 0
                    trip_pre_rows[t_id] = {
                        "route_id": r_id,
                        "service_id": row.get("service_id", "").strip(),
                        "trip_headsign": row.get("trip_headsign", "").strip(),
                        "direction_id": direction_id,
                    }

        logger.info("Found %d trips for target routes", len(target_trip_ids))

        # 4. Stream stop_times for target trips to compute start/end bounds in low memory
        # Maps trip_id -> [min_seq, first_stop_id, start_dep, max_seq, last_stop_id, end_arr, count]
        trip_bounds: dict[str, list[Any]] = {}
        target_stop_ids: set[str] = set()
        stop_time_rows: list[tuple[str, int, str, int, int]] = []
        stop_times_processed = 0

        with archive.open("stop_times.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                t_id = row.get("trip_id", "").strip()
                if t_id not in target_trip_ids:
                    continue

                seq = int(row["stop_sequence"])
                s_id = row["stop_id"].strip()
                target_stop_ids.add(s_id)
                stop_times_processed += 1

                bounds = trip_bounds.get(t_id)
                if bounds is None:
                    arr_sec = time_str_to_seconds(row["arrival_time"])
                    dep_sec = time_str_to_seconds(row["departure_time"])
                    trip_bounds[t_id] = [seq, s_id, dep_sec, seq, s_id, arr_sec, 1]
                    if store_stop_times:
                        stop_time_rows.append((t_id, seq, s_id, arr_sec, dep_sec))
                else:
                    bounds[6] += 1
                    arr_sec = None
                    dep_sec = None
                    if seq < bounds[0]:
                        dep_sec = time_str_to_seconds(row["departure_time"])
                        bounds[0] = seq
                        bounds[1] = s_id
                        bounds[2] = dep_sec
                    if seq > bounds[3]:
                        arr_sec = time_str_to_seconds(row["arrival_time"])
                        bounds[3] = seq
                        bounds[4] = s_id
                        bounds[5] = arr_sec
                    if store_stop_times:
                        if arr_sec is None:
                            arr_sec = time_str_to_seconds(row["arrival_time"])
                        if dep_sec is None:
                            dep_sec = time_str_to_seconds(row["departure_time"])
                        stop_time_rows.append((t_id, seq, s_id, arr_sec, dep_sec))

        # Build final static_trips records
        trip_final_rows = []
        for t_id, bounds in trip_bounds.items():
            pre = trip_pre_rows[t_id]
            trip_final_rows.append((
                t_id,
                pre["route_id"],
                pre["service_id"],
                pre["trip_headsign"],
                pre["direction_id"],
                bounds[2],  # start_dep
                bounds[5],  # end_arr
                bounds[1],  # first_stop_id
                bounds[4],  # last_stop_id
                bounds[6],  # total_stops
            ))

        # Free temporary dicts before DB insert
        trip_bounds.clear()
        trip_pre_rows.clear()
        target_trip_ids.clear()

        # 5. Parse Stops
        stop_rows = []
        with archive.open("stops.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                s_id = row.get("stop_id", "").strip()
                if s_id in target_stop_ids:
                    lat_str = row.get("stop_lat", "").strip()
                    lon_str = row.get("stop_lon", "").strip()
                    stop_rows.append((
                        s_id,
                        row.get("stop_code", "").strip() or None,
                        row.get("stop_name", "").strip(),
                        float(lat_str) if lat_str else None,
                        float(lon_str) if lon_str else None,
                    ))

        # 6. Insert all records in transaction
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO static_agencies VALUES (?, ?, ?);",
                agency_rows,
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO static_routes VALUES (?, ?, ?, ?, ?);",
                route_rows,
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO static_stops VALUES (?, ?, ?, ?, ?);",
                stop_rows,
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO static_trips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                trip_final_rows,
            )
            if store_stop_times and stop_time_rows:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO static_trip_stop_times VALUES (?, ?, ?, ?, ?);",
                    stop_time_rows,
                )

        counts["agencies"] = len(agency_rows)
        counts["routes"] = len(route_rows)
        counts["stops"] = len(stop_rows)
        counts["trips"] = len(trip_final_rows)
        counts["stop_times"] = stop_times_processed
        logger.info("Ingested GTFS static data successfully: %s", counts)
        return counts

    def get_trip_details(self, trip_id: str) -> dict[str, Any] | None:
        """Fetch static trip metadata including schedule bounds, route, headsign and first stop name."""
        cursor = self.conn.execute("""
            SELECT t.trip_id, t.route_id, r.agency_id, t.direction_id,
                   t.scheduled_start_seconds, t.scheduled_end_seconds,
                   t.first_stop_id, t.last_stop_id, t.total_stops,
                   t.trip_headsign,
                   s.stop_name AS first_stop_name
            FROM static_trips t
            JOIN static_routes r ON t.route_id = r.route_id
            LEFT JOIN static_stops s ON t.first_stop_id = s.stop_id
            WHERE t.trip_id = ?;
        """, (trip_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_trip_stops(self, trip_id: str) -> list[dict[str, Any]]:
        """Fetch ordered stop times for a trip."""
        cursor = self.conn.execute("""
            SELECT stop_sequence, stop_id, arrival_seconds, departure_seconds
            FROM static_trip_stop_times
            WHERE trip_id = ?
            ORDER BY stop_sequence ASC;
        """, (trip_id,))
        return [dict(r) for r in cursor.fetchall()]
