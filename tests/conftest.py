"""Test fixtures and database helpers for pytest."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from src.config import Config
from src.database import get_db_connection, init_db


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Provide a path to a temporary database file."""
    return tmp_path / "test_nta.db"


@pytest.fixture
def db_conn(temp_db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Provide an initialized SQLite database in WAL mode."""
    conn = get_db_connection(temp_db_path)
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def test_config(temp_db_path: Path) -> Config:
    """Config pointing to temporary test database."""
    return Config(
        nta_api_key="test_dummy_key",
        poll_interval_seconds=65,
        target_agencies_raw="Dublin Bus,Go-Ahead Ireland,Iarnród Éireann,Luas",
        db_path=temp_db_path,
        ghost_grace_minutes=15,
    )


@pytest.fixture
def seed_static_data(db_conn: sqlite3.Connection):
    """Seed test database with sample Dublin Bus, Irish Rail, and Luas routes and trips."""
    with db_conn:
        # Agencies
        db_conn.executemany(
            "INSERT INTO static_agencies VALUES (?, ?, ?);",
            [
                ("bac", "Dublin Bus", "Europe/Dublin"),
                ("gae", "Go-Ahead Ireland", "Europe/Dublin"),
                ("ir", "Iarnród Éireann", "Europe/Dublin"),
                ("luas", "Luas", "Europe/Dublin"),
            ],
        )

        # Routes
        db_conn.executemany(
            "INSERT INTO static_routes VALUES (?, ?, ?, ?, ?);",
            [
                ("route_39A", "bac", "39A", "Ongar to UCD Belfield", 3),
                ("route_38D", "bac", "38D", "Damastown to Burlington Rd", 3),
                ("route_70", "bac", "70", "Dunboyne to Burlington Rd", 3),
                ("route_DART", "ir", "DART", "Howth/Malahide to Greystones", 2),
                ("route_LUAS_RED", "luas", "Luas Red", "Tallaght to The Point", 0),
            ],
        )

        # Stops
        db_conn.executemany(
            "INSERT INTO static_stops VALUES (?, ?, ?, ?, ?);",
            [
                ("stop_1", "1001", "Start Terminus", 53.35, -6.26),
                ("stop_2", "1002", "Corridor Stop A", 53.36, -6.27),
                ("stop_3", "1003", "Corridor Stop B", 53.37, -6.28),
                ("stop_4", "1004", "City Quays Stop", 53.38, -6.29),
                ("stop_5", "1005", "End Terminus", 53.39, -6.30),
                ("stop_start", "2001", "Damastown Gate", 53.40, -6.35),
                ("stop_corridor", "2002", "Blanchardstown SC", 53.39, -6.38),
                ("stop_quays_choke", "2003", "Bachelors Walk", 53.34, -6.26),
                ("stop_suburb", "2004", "Leeson St Upper", 53.33, -6.25),
                ("stop_terminus", "2005", "Burlington Road", 53.33, -6.24),
            ],
        )

        # Trips
        # 1. On time trip: 5 stops, start 08:00 (28800), end 08:45 (31500)
        # 2. Padding recovery trip: 5 stops, start 08:15 (29700), end 09:15 (33300)
        # 3. Late trip: 3 stops, start 08:30 (30600), end 09:00 (32400)
        # 4. Canceled trip: 5 stops
        # 5. Ghost trip: 5 stops, start 07:00 (25200), end 07:45 (27900)
        # 6. Terminated early trip: 10 stops, start 08:00 (28800), end 09:00 (32400)
        db_conn.executemany(
            "INSERT INTO static_trips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            [
                ("trip_on_time_1", "route_39A", "srv_1", "UCD Belfield", 0, 28800, 31500, "stop_1", "stop_5", 5),
                ("trip_padding_1", "route_38D", "srv_1", "Burlington Road", 0, 29700, 33300, "stop_start", "stop_terminus", 5),
                ("trip_late_1", "route_70", "srv_1", "Burlington Road", 0, 30600, 32400, "stop_1", "stop_3", 3),
                ("trip_canceled_1", "route_LUAS_RED", "srv_1", "The Point", 0, 28800, 32400, "stop_1", "stop_5", 5),
                ("trip_ghost_1", "route_DART", "srv_1", "Greystones", 0, 25200, 27900, "stop_1", "stop_5", 5),
                ("trip_terminated_early_1", "route_39A", "srv_1", "UCD Belfield", 0, 28800, 32400, "stop_1", "stop_5", 10),
            ],
        )

        # Stop Times for on-time and padding trips
        db_conn.executemany(
            "INSERT INTO static_trip_stop_times VALUES (?, ?, ?, ?, ?);",
            [
                ("trip_on_time_1", 1, "stop_1", 28800, 28800),
                ("trip_on_time_1", 2, "stop_2", 29400, 29400),
                ("trip_on_time_1", 3, "stop_3", 30000, 30000),
                ("trip_on_time_1", 4, "stop_4", 30600, 30600),
                ("trip_on_time_1", 5, "stop_5", 31500, 31500),
                ("trip_padding_1", 1, "stop_start", 29700, 29700),
                ("trip_padding_1", 2, "stop_corridor", 30600, 30600),
                ("trip_padding_1", 3, "stop_quays_choke", 31500, 31500),
                ("trip_padding_1", 4, "stop_suburb", 32400, 32400),
                ("trip_padding_1", 5, "stop_terminus", 33300, 33300),
            ],
        )
