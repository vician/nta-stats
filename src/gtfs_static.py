"""GTFS Static schedule ingestion, agency filtering, and SQLite caching."""

from __future__ import annotations

import csv
import io
import logging
import sqlite3
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

    def download_and_ingest(self, zip_source: str | Path | None = None) -> dict[str, int]:
        """Download or read GTFS zip, parse target agencies, and populate SQLite tables.
        
        Args:
            zip_source: Local file path or URL. Defaults to config.gtfs_static_url.
            
        Returns:
            Dictionary with counts of ingested entities.
        """
        if zip_source is None:
            zip_source = self.config.gtfs_static_url

        if isinstance(zip_source, str) and zip_source.startswith("http"):
            logger.info("Downloading GTFS static archive from %s...", zip_source)
            resp = requests.get(zip_source, timeout=120)
            resp.raise_for_status()
            zip_bytes = io.BytesIO(resp.content)
            archive = zipfile.ZipFile(zip_bytes)
        else:
            archive = zipfile.ZipFile(zip_source)

        with archive:
            return self._parse_and_insert(archive)

    def _parse_and_insert(self, archive: zipfile.ZipFile) -> dict[str, int]:
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

        # 4. Parse stop_times for target trips to compute start/end bounds and sequences
        trip_stop_times: dict[str, list[tuple[int, str, int, int]]] = {}
        target_stop_ids: set[str] = set()

        with archive.open("stop_times.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                t_id = row.get("trip_id", "").strip()
                if t_id in target_trip_ids:
                    seq = int(row["stop_sequence"])
                    s_id = row["stop_id"].strip()
                    arr_sec = time_str_to_seconds(row["arrival_time"])
                    dep_sec = time_str_to_seconds(row["departure_time"])
                    
                    target_stop_ids.add(s_id)
                    if t_id not in trip_stop_times:
                        trip_stop_times[t_id] = []
                    trip_stop_times[t_id].append((seq, s_id, arr_sec, dep_sec))

        # Build final static_trips and static_trip_stop_times records
        trip_final_rows = []
        stop_time_rows = []

        for t_id, stops in trip_stop_times.items():
            stops.sort(key=lambda x: x[0])  # Sort by sequence
            first_seq, first_stop_id, start_arr, start_dep = stops[0]
            last_seq, last_stop_id, end_arr, end_dep = stops[-1]
            pre = trip_pre_rows[t_id]

            trip_final_rows.append((
                t_id,
                pre["route_id"],
                pre["service_id"],
                pre["trip_headsign"],
                pre["direction_id"],
                start_dep,
                end_arr,
                first_stop_id,
                last_stop_id,
                len(stops),
            ))

            for seq, s_id, arr_s, dep_s in stops:
                stop_time_rows.append((t_id, seq, s_id, arr_s, dep_s))

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
            self.conn.executemany(
                "INSERT OR REPLACE INTO static_trip_stop_times VALUES (?, ?, ?, ?, ?);",
                stop_time_rows,
            )

        counts["agencies"] = len(agency_rows)
        counts["routes"] = len(route_rows)
        counts["stops"] = len(stop_rows)
        counts["trips"] = len(trip_final_rows)
        counts["stop_times"] = len(stop_time_rows)
        logger.info("Ingested GTFS static data successfully: %s", counts)
        return counts

    def get_trip_details(self, trip_id: str) -> dict[str, Any] | None:
        """Fetch static trip metadata including schedule bounds and route."""
        cursor = self.conn.execute("""
            SELECT t.trip_id, t.route_id, r.agency_id, t.direction_id,
                   t.scheduled_start_seconds, t.scheduled_end_seconds,
                   t.first_stop_id, t.last_stop_id, t.total_stops
            FROM static_trips t
            JOIN static_routes r ON t.route_id = r.route_id
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
