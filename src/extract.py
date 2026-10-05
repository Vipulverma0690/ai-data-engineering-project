"""Extraction layer: Open-Meteo API -> raw JSON files in data/raw/.

- Retries transient failures (timeouts, connection errors, HTTP 429/5xx) with exponential backoff.
- Does NOT retry client errors (HTTP 4xx) because retrying a bad request will not fix it.
- Caches each response on disk; re-runs reuse the cache unless refresh=True.
- One failed city/source never stops the whole run; every outcome is written to a manifest.
"""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from src import config

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MANIFEST_PATH = config.RAW_DIR / "_extract_manifest.json"


class ExtractError(Exception):
    """Raised when an API call fails permanently."""


def fetch_json(url: str, params: dict) -> dict:
    """GET a URL and return parsed JSON, retrying transient failures."""
    last_error = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SEC)

            if resp.status_code in RETRYABLE_STATUS:
                raise requests.HTTPError(f"HTTP {resp.status_code} (retryable)", response=resp)
            if resp.status_code >= 400:
                # Open-Meteo returns {"error": true, "reason": "..."} for bad requests
                raise ExtractError(f"HTTP {resp.status_code}: {resp.text[:300]}")

            data = resp.json()
            if isinstance(data, dict) and data.get("error"):
                raise ExtractError(f"API error: {data.get('reason')}")
            return data

        except ExtractError:
            raise  # permanent failure: do not retry
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt < config.MAX_RETRIES:
                wait = config.BACKOFF_SEC * (2 ** (attempt - 1))
                logger.warning("Attempt %d/%d failed (%s). Retrying in %ss...",
                               attempt, config.MAX_RETRIES, exc, wait)
                time.sleep(wait)

    raise ExtractError(f"Failed after {config.MAX_RETRIES} attempts: {last_error}")


def build_requests(city: dict) -> dict:
    """Return {source_name: (url, params)} for one city."""
    common = {
        "latitude": city["latitude"],
        "longitude": city["longitude"],
        "start_date": config.START_DATE,
        "end_date": config.END_DATE,
        "timezone": config.TIMEZONE,
    }
    return {
        "weather": (config.WEATHER_URL,
                    {**common, "daily": ",".join(config.WEATHER_DAILY_VARS)}),
        "air_quality": (config.AIR_QUALITY_URL,
                        {**common, "hourly": ",".join(config.AIR_QUALITY_HOURLY_VARS)}),
    }


def raw_path(source: str, city_id: str) -> Path:
    return config.RAW_DIR / f"{source}_{city_id}_{config.START_DATE}_{config.END_DATE}.json"


def is_valid_cache(path: Path) -> bool:
    """A cached file is usable only if it exists and parses as JSON with a payload."""
    if not path.exists():
        return False
    try:
        with open(path, encoding="utf-8") as f:
            return "payload" in json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt cache file %s - will re-download.", path.name)
        return False


def save_raw(path: Path, url: str, params: dict, payload: dict) -> None:
    """Write payload plus request metadata. Write to a temp file first so a crash never leaves a half-written file."""
    record = {
        "_meta": {
            "source_url": url,
            "params": params,
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "payload": payload,
    }
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
    tmp.replace(path)


def run_extraction(refresh: bool = False) -> list[dict]:
    """Extract all cities and sources. Returns a manifest (one entry per city x source)."""
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []

    for city in config.CITIES:
        for source, (url, params) in build_requests(city).items():
            path = raw_path(source, city["city_id"])
            entry = {"city_id": city["city_id"], "source": source, "file": path.name}

            if not refresh and is_valid_cache(path):
                logger.info("[cache]   %-11s %s", source, city["city"])
                entry["status"] = "cached"
            else:
                try:
                    payload = fetch_json(url, params)
                    save_raw(path, url, params, payload)
                    logger.info("[fetched] %-11s %s", source, city["city"])
                    entry["status"] = "fetched"
                    time.sleep(0.5)  # be polite to the free API
                except ExtractError as exc:
                    logger.error("[failed]  %-11s %s: %s", source, city["city"], exc)
                    entry["status"] = "failed"
                    entry["error"] = str(exc)

            manifest.append(entry)

    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump({"run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "entries": manifest}, f, indent=2)
    return manifest