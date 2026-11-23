# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Set environment variables for Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install system dependencies if required (sqlite3, ca-certificates)
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ /app/src/

# Ensure data directory exists
RUN mkdir -p /app/data

# Default entrypoint runs the telemetry poller daemon
CMD ["python", "-m", "src.poller"]
