"""Configuration management for NTA Stats Tracker."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_env_file(env_path: Path) -> None:
    """Simple parser to load .env file into os.environ if not already present."""
    if not env_path.is_file():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("\"'")
            if key not in os.environ:
                os.environ[key] = val


@dataclass(frozen=True)
class Config:
    """Application configuration loaded from environment and defaults."""

    nta_api_key: str = field(
        default_factory=lambda: os.getenv("NTA_API_KEY", "")
    )
    poll_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("POLL_INTERVAL_SECONDS", "65"))
    )
    target_agencies_raw: str = field(
        default_factory=lambda: os.getenv(
            "TARGET_AGENCIES",
            "Dublin Bus,Go-Ahead Ireland,Iarnród Éireann,Luas",
        )
    )
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("DB_PATH", "data/nta_stats.db"))
    )
    gtfs_static_url: str = field(
        default_factory=lambda: os.getenv(
            "GTFS_STATIC_URL",
            "https://www.transportforireland.ie/transitData/Data/GTFS_All.zip",
        )
    )
    ghost_grace_minutes: int = field(
        default_factory=lambda: int(os.getenv("GHOST_GRACE_MINUTES", "20"))
    )

    @property
    def target_agencies(self) -> list[str]:
        """Parsed list of target agency names or keywords."""
        return [
            agency.strip().lower()
            for agency in self.target_agencies_raw.split(",")
            if agency.strip()
        ]

    def matches_target_agency(self, agency_id: str, agency_name: str) -> bool:
        """Check if an agency ID or name matches configured target agencies.
        
        Common NTA Agency mappings:
          - Dublin Bus: '978', 'bac', 'Dublin Bus'
          - Go-Ahead Ireland: 'gae', 'Go-Ahead Ireland'
          - Irish Rail: 'ir', 'Iarnród Éireann', 'Irish Rail'
          - Luas: 'luas', 'Luas'
        """
        id_lower = agency_id.lower().strip()
        name_lower = agency_name.lower().strip()

        # Known mapping aliases
        known_aliases = {
            "978": ["dublin bus", "bac"],
            "bac": ["dublin bus", "978"],
            "gae": ["go-ahead", "go-ahead ireland"],
            "ir": ["irish rail", "iarnród éireann", "iarnrod eireann"],
            "luas": ["luas"],
        }

        for target in self.target_agencies:
            target = target.lower()
            if target in id_lower or target in name_lower:
                return True
            # Check aliases
            for key, aliases in known_aliases.items():
                if target in key or any(target in alias for alias in aliases):
                    if key == id_lower or any(alias in name_lower for alias in aliases):
                        return True

        return False


def get_config(env_path: Path | None = None) -> Config:
    """Load and return application configuration."""
    if env_path is None:
        env_path = Path(".env")
    _load_env_file(env_path)
    return Config()
