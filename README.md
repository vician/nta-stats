# Irish Public Transport Reliability & Delay Tracker (`nta-stats`)

A high-performance, containerized telemetry and reliability engine for Irish public transit: **Dublin Bus**, **Go-Ahead Ireland**, **Irish Rail (DART & Commuter)**, and **Luas**, powered by the National Transport Authority (NTA) GTFS Static and GTFS-Realtime v2 feeds.

---

## Key Features

- **In-Memory Trip State Machine**: Tracks thousands of concurrent vehicles in memory without thrashing SQLite on every poll tick. Writes exactly one finalized record upon trip completion, cancellation, early termination, or ghost expiration.
- **5-Way Outcome Classification**:
  - `ON_TIME`: Terminus arrival within $-60\text{s}$ to $+300\text{s}$ (5 minutes) of schedule.
  - `LATE`: Terminus arrival delayed $> +300\text{s}$.
  - `CANCELED`: Explicitly flagged `CANCELED` in the GTFS-R feed.
  - `GHOST`: Scheduled trip that never appeared in the GPS feed and vanished without operating.
  - `TERMINATED_EARLY`: Bus/train stopped reporting or cut short before its scheduled terminus.
- **Timetable Padding Detection**: Concurrently tracks both peak mid-route delay (`max_delay_seconds`, `max_delay_stop_id`) and final terminus delay (`end_delay_seconds`), preventing operators from masking 15+ minute quays delays behind padded terminal buffers.
- **Bottleneck Isolation (`stop_daily_stats`)**: Records added segment delay ($\Delta \text{delay}_{A \to B} = \text{delay}_B - \text{delay}_A$) to identify street-level congestion hotspots.
- **SQLite in WAL Mode**: Configured with `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, and `PRAGMA busy_timeout=5000;` for concurrent reading and writing.
- **NTA Fair Usage Compliant**: Defaults to a `65s` poll interval to strictly respect the NTA Fair Usage Policy ($\le 1\text{ req}/60\text{s}$).

---

## Project Structure

```
nta-stats/
├── Dockerfile                   # Python 3.12-slim production image
├── docker-compose.yml           # Compose setup (poller, volume mounts, optional Grafana)
├── pyproject.toml               # Python dependencies and pytest configuration
├── requirements.txt             # Core pip dependencies
├── .env.example                 # Environment variable template
├── src/
│   ├── config.py                # Environment configuration & agency matcher
│   ├── database.py              # SQLite connection, WAL pragmas, batch persistence
│   ├── models.py                # Enums, Dataclasses for trips and daily rollups
│   ├── gtfs_static.py           # Static GTFS schedule downloader, parser, and lookup cache
│   ├── tracker.py               # In-memory trip state machine & garbage collector
│   └── poller.py                # Polling daemon orchestration loop & signal handlers
└── tests/
    ├── fixtures/
    │   └── gtfs_realtime_mocks.py # Realistic mock payloads (ON_TIME, LATE, CANCELED, GHOST, EARLY_TERM)
    ├── conftest.py              # Pytest fixtures and seeded static schedule
    ├── test_database.py         # WAL pragmas, insertion, and rollup tests
    ├── test_gtfs_static.py      # Agency filtering and schedule parsing tests
    └── test_state_machine.py    # State-machine transitions and 5-way classification tests
```

---

## Quickstart

### 1. Prerequisites & NTA API Key
Register for a free API key on the [NTA Developer Portal](https://developer.nationaltransport.ie/).

Copy the configuration template:
```bash
cp .env.example .env
```
Edit `.env` and enter your `NTA_API_KEY`:
```ini
NTA_API_KEY=your_primary_or_secondary_key_here
POLL_INTERVAL_SECONDS=65
TARGET_AGENCIES=Dublin Bus,Go-Ahead Ireland,Iarnród Éireann,Luas
DB_PATH=data/nta_stats.db
```

### 2. Local Python Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the test suite
pytest -v
```

### 3. Run with Docker Compose
```bash
docker compose up -d
```
Inspect poller logs:
```bash
docker compose logs -f poller
```

---

## Verification & Test Scenarios

The test suite validates:
1. **On-Time Operation**: Trips reaching terminus within $[-60\text{s}, +300\text{s}]$ are classified `ON_TIME`.
2. **Padding Recovery**: Bus delayed by $+15\text{ min}$ along city quays (`max_delay_seconds = 900`) that recovers at terminus (`end_delay_seconds = 120`) preserves both metrics and isolates segment delay.
3. **Delayed Trip**: Bus delayed $> 300\text{s}$ at terminus classified `LATE`.
4. **Explicit Cancellation**: Marked `CANCELED` directly from GTFS-R feed.
5. **Ghost Detection**: Scheduled trip never appearing with GPS pings classified `GHOST` with missing stop counts incremented.
6. **Early Termination**: Short-turned trips classified `TERMINATED_EARLY` with `is_short_turned = 1`.

Run tests anytime with:
```bash
pytest -v
```

---

## Attribution & Disclaimers

Data provided under the [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/) license by the **National Transport Authority (NTA)** / Transport for Ireland (TFI). Data is provided *"as is"* without warranties.
