"""Tests for GTFS static parsing, agency filtering, and time arithmetic."""

from __future__ import annotations

import io
import sqlite3
import zipfile

from src.config import Config
from src.gtfs_static import (
    GTFSStaticManager,
    seconds_to_time_str,
    time_str_to_seconds,
)


def test_time_conversions():
    """Verify standard and overnight GTFS time conversions."""
    assert time_str_to_seconds("08:30:15") == 8 * 3600 + 30 * 60 + 15
    assert time_str_to_seconds("24:15:00") == 24 * 3600 + 15 * 60
    assert time_str_to_seconds("26:05:00") == 26 * 3600 + 5 * 60

    assert seconds_to_time_str(30615) == "08:30:15"
    assert seconds_to_time_str(87300) == "24:15:00"


def test_agency_matching():
    """Verify filtering targets Dublin Bus, Go-Ahead, Irish Rail, and Luas."""
    cfg = Config(target_agencies_raw="Dublin Bus,Go-Ahead Ireland,Iarnród Éireann,Luas")

    assert cfg.matches_target_agency("bac", "Dublin Bus")
    assert cfg.matches_target_agency("978", "Dublin Bus")
    assert cfg.matches_target_agency("gae", "Go-Ahead Ireland")
    assert cfg.matches_target_agency("ir", "Iarnród Éireann")
    assert cfg.matches_target_agency("ir", "Irish Rail")
    assert cfg.matches_target_agency("luas", "Luas")

    # Non-target agency
    assert not cfg.matches_target_agency("be", "Bus Éireann")
    assert not cfg.matches_target_agency("translink", "Translink")


def test_static_zip_ingestion(db_conn: sqlite3.Connection):
    """Verify parsing an in-memory GTFS zip archive filters and populates SQLite."""
    # Build sample zip in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as z:
        z.writestr(
            "agency.txt",
            "agency_id,agency_name,agency_url,agency_timezone\n"
            "bac,Dublin Bus,https://dublinbus.ie,Europe/Dublin\n"
            "be,Bus Eireann,https://buseireann.ie,Europe/Dublin\n",  # Non-target
        )
        z.writestr(
            "routes.txt",
            "route_id,agency_id,route_short_name,route_long_name,route_type\n"
            "r_39A,bac,39A,Ongar to Belfield,3\n"
            "r_100X,be,100X,Dundalk to Dublin,3\n",  # Should be excluded
        )
        z.writestr(
            "stops.txt",
            "stop_id,stop_code,stop_name,stop_lat,stop_lon\n"
            "s_ongar,1001,Ongar Terminus,53.39,-6.42\n"
            "s_ucd,1002,Belfield UCD,53.30,-6.22\n"
            "s_dundalk,2001,Dundalk Station,54.00,-6.40\n",
        )
        z.writestr(
            "trips.txt",
            "route_id,service_id,trip_id,trip_headsign,direction_id\n"
            "r_39A,s1,t_39A_1,Belfield UCD,0\n"
            "r_100X,s1,t_100X_1,Dublin Airport,0\n",
        )
        z.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "t_39A_1,08:00:00,08:00:00,s_ongar,1\n"
            "t_39A_1,08:50:00,08:50:00,s_ucd,2\n"
            "t_100X_1,08:00:00,08:00:00,s_dundalk,1\n",
        )

    zip_buffer.seek(0)
    cfg = Config(target_agencies_raw="Dublin Bus")
    manager = GTFSStaticManager(cfg, db_conn)
    counts = manager.download_and_ingest(zip_buffer)

    assert counts["agencies"] == 1
    assert counts["routes"] == 1
    assert counts["trips"] == 1
    assert counts["stops"] == 2
    assert counts["stop_times"] == 2

    # Query trip details
    details = manager.get_trip_details("t_39A_1")
    assert details is not None
    assert details["route_id"] == "r_39A"
    assert details["agency_id"] == "bac"
    assert details["total_stops"] == 2
    assert details["scheduled_start_seconds"] == 28800
    assert details["scheduled_end_seconds"] == 31800
