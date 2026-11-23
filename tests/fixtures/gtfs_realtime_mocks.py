"""Realistic mock NTA GTFS-Realtime v2 JSON payloads for testing."""

from __future__ import annotations

from typing import Any


def create_vehicle_entity(
    trip_id: str,
    vehicle_id: str,
    timestamp: int,
    stop_id: str | None = None,
    current_stop_sequence: int | None = None,
) -> dict[str, Any]:
    """Create a mock GTFS-R VehiclePosition entity."""
    return {
        "id": f"veh_ent_{vehicle_id}",
        "vehicle": {
            "trip": {
                "trip_id": trip_id,
                "schedule_relationship": "SCHEDULED",
            },
            "vehicle": {
                "id": vehicle_id,
                "label": f"BUS-{vehicle_id}",
            },
            "position": {
                "latitude": 53.3498,
                "longitude": -6.2603,
            },
            "current_stop_sequence": current_stop_sequence,
            "stop_id": stop_id,
            "timestamp": timestamp,
        },
    }


def create_trip_update_entity(
    trip_id: str,
    vehicle_id: str | None = None,
    schedule_relationship: str = "SCHEDULED",
    stop_time_updates: list[dict[str, Any]] | None = None,
    timestamp: int = 1700000000,
) -> dict[str, Any]:
    """Create a mock GTFS-R TripUpdate entity."""
    entity: dict[str, Any] = {
        "id": f"tu_ent_{trip_id}",
        "trip_update": {
            "trip": {
                "trip_id": trip_id,
                "schedule_relationship": schedule_relationship,
            },
            "timestamp": timestamp,
            "stop_time_update": stop_time_updates or [],
        },
    }
    if vehicle_id:
        entity["trip_update"]["vehicle"] = {
            "id": vehicle_id,
            "label": f"BUS-{vehicle_id}",
        }
    return entity


def build_on_time_trip_payload(
    trip_id: str = "trip_on_time_1",
    vehicle_id: str = "BUS_101",
    num_stops: int = 5,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generates a trip that stays within -60s to +300s of schedule."""
    stops = []
    # Delays: 0s, +30s, +45s, +60s, +90s (all well under 300s)
    delays = [0, 30, 45, 60, 90]
    for i in range(1, num_stops + 1):
        stops.append({
            "stop_sequence": i,
            "stop_id": f"stop_{i}",
            "arrival": {"delay": delays[i - 1]},
            "departure": {"delay": delays[i - 1]},
        })

    tu_feed = {
        "header": {"gtfs_realtime_version": "2.0"},
        "entity": [create_trip_update_entity(trip_id, vehicle_id, "SCHEDULED", stops)],
    }
    veh_feed = {
        "header": {"gtfs_realtime_version": "2.0"},
        "entity": [create_vehicle_entity(trip_id, vehicle_id, 1700000000, "stop_5", 5)],
    }
    return tu_feed, veh_feed


def build_padding_recovery_trip_payload(
    trip_id: str = "trip_padding_1",
    vehicle_id: str = "BUS_102",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bus delayed mid-route (+15 mins / +900s), but recovers via padding at terminus (+120s)."""
    stops = [
        {"stop_sequence": 1, "stop_id": "stop_start", "arrival": {"delay": 0}},
        {"stop_sequence": 2, "stop_id": "stop_corridor", "arrival": {"delay": 450}},
        {"stop_sequence": 3, "stop_id": "stop_quays_choke", "arrival": {"delay": 900}},  # Peak delay
        {"stop_sequence": 4, "stop_id": "stop_suburb", "arrival": {"delay": 600}},
        {"stop_sequence": 5, "stop_id": "stop_terminus", "arrival": {"delay": 120}},    # Made up time!
    ]
    tu_feed = {
        "header": {"gtfs_realtime_version": "2.0"},
        "entity": [create_trip_update_entity(trip_id, vehicle_id, "SCHEDULED", stops)],
    }
    veh_feed = {
        "header": {"gtfs_realtime_version": "2.0"},
        "entity": [create_vehicle_entity(trip_id, vehicle_id, 1700000000, "stop_terminus", 5)],
    }
    return tu_feed, veh_feed


def build_late_trip_payload(
    trip_id: str = "trip_late_1",
    vehicle_id: str = "BUS_103",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bus arrives at terminus with +450s delay (> 300s limit)."""
    stops = [
        {"stop_sequence": 1, "stop_id": "stop_1", "arrival": {"delay": 100}},
        {"stop_sequence": 2, "stop_id": "stop_2", "arrival": {"delay": 250}},
        {"stop_sequence": 3, "stop_id": "stop_3", "arrival": {"delay": 450}},
    ]
    tu_feed = {
        "header": {"gtfs_realtime_version": "2.0"},
        "entity": [create_trip_update_entity(trip_id, vehicle_id, "SCHEDULED", stops)],
    }
    veh_feed = {
        "header": {"gtfs_realtime_version": "2.0"},
        "entity": [create_vehicle_entity(trip_id, vehicle_id, 1700000000, "stop_3", 3)],
    }
    return tu_feed, veh_feed


def build_canceled_trip_payload(
    trip_id: str = "trip_canceled_1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Trip explicitly marked CANCELED in GTFS-R feed."""
    tu_feed = {
        "header": {"gtfs_realtime_version": "2.0"},
        "entity": [create_trip_update_entity(trip_id, None, "CANCELED", [])],
    }
    veh_feed = {"header": {"gtfs_realtime_version": "2.0"}, "entity": []}
    return tu_feed, veh_feed


def build_terminated_early_trip_payload(
    trip_id: str = "trip_terminated_early_1",
    vehicle_id: str = "BUS_104",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Trip scheduled for 10 stops, but vehicle stopped reporting at stop sequence 4."""
    stops = [
        {"stop_sequence": 1, "stop_id": "stop_1", "arrival": {"delay": 30}},
        {"stop_sequence": 2, "stop_id": "stop_2", "arrival": {"delay": 60}},
        {"stop_sequence": 3, "stop_id": "stop_3", "arrival": {"delay": 120}},
        {"stop_sequence": 4, "stop_id": "stop_4", "arrival": {"delay": 180}},
    ]
    tu_feed = {
        "header": {"gtfs_realtime_version": "2.0"},
        "entity": [create_trip_update_entity(trip_id, vehicle_id, "SCHEDULED", stops)],
    }
    veh_feed = {
        "header": {"gtfs_realtime_version": "2.0"},
        "entity": [create_vehicle_entity(trip_id, vehicle_id, 1700000000, "stop_4", 4)],
    }
    return tu_feed, veh_feed
