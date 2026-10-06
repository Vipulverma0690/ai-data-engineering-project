"""Transformation layer: raw JSON -> clean, modelled tables.

Flow (called from etl.py):
    1. load_sources()      raw JSON files      -> weather_daily_raw, aq_hourly_raw  (untouched values)
    2. clean_weather() / clean_air_quality()   -> dedupe, null-out impossible values
    3. aggregate_air_quality()                 hourly -> daily
    4. build_dim_city() / build_dim_date()
    5. build_fact_city_daily()                 calendar spine + joins + risk flags
    6. build_mart_city_month()                 monthly KPIs for the dashboard

Every function is pure (DataFrame in -> DataFrame out) so it can be unit-tested
without touching the API or the disk.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src import config
from src.extract import raw_path

logger = logging.getLogger(__name__)

# API field name -> our column name
WEATHER_COLUMNS = {
    "temperature_2m_max": "temp_max",
    "temperature_2m_min": "temp_min",
    "temperature_2m_mean": "temp_mean",
    "precipitation_sum": "precipitation_mm",
    "wind_speed_10m_max": "wind_max_kmh",
}
AQ_COLUMNS = {"pm2_5": "pm25", "pm10": "pm10"}
WEATHER_MEASURES = list(WEATHER_COLUMNS.values())

SEASONS = {  # IMD seasons
    1: "Winter", 2: "Winter",
    3: "Pre-monsoon", 4: "Pre-monsoon", 5: "Pre-monsoon",
    6: "Monsoon", 7: "Monsoon", 8: "Monsoon", 9: "Monsoon",
    10: "Post-monsoon", 11: "Post-monsoon", 12: "Post-monsoon",
}


# ---------------------------------------------------------------------------
# 1. Parse raw JSON
# ---------------------------------------------------------------------------
def parse_weather(payload: dict, city_id: str, source_file: str) -> pd.DataFrame:
    """Open-Meteo 'daily' block -> one row per day."""
    daily = payload.get("daily")
    if not daily or "time" not in daily:
        raise ValueError(f"{source_file}: no 'daily' data in payload")

    df = pd.DataFrame({"date": pd.to_datetime(daily["time"])})
    for api_name, col in WEATHER_COLUMNS.items():
        # A variable missing from the response becomes an all-null column (caught by DQ)
        df[col] = pd.to_numeric(pd.Series(daily.get(api_name, [None] * len(df))), errors="coerce")
    df.insert(0, "city_id", city_id)
    df["source_file"] = source_file
    return df


def parse_air_quality(payload: dict, city_id: str, source_file: str) -> pd.DataFrame:
    """Open-Meteo 'hourly' block -> one row per hour."""
    hourly = payload.get("hourly")
    if not hourly or "time" not in hourly:
        raise ValueError(f"{source_file}: no 'hourly' data in payload")

    df = pd.DataFrame({"timestamp": pd.to_datetime(hourly["time"])})
    for api_name, col in AQ_COLUMNS.items():
        df[col] = pd.to_numeric(pd.Series(hourly.get(api_name, [None] * len(df))), errors="coerce")
    df.insert(0, "city_id", city_id)
    df["source_file"] = source_file
    return df


def load_sources() -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Read every expected raw file. Missing/unreadable files are recorded, not fatal."""
    weather_frames, aq_frames, availability = [], [], []

    for city in config.CITIES:
        cid = city["city_id"]
        for source, parser, bucket in (
            ("weather", parse_weather, weather_frames),
            ("air_quality", parse_air_quality, aq_frames),
        ):
            path = raw_path(source, cid)
            entry = {"city_id": cid, "source": source, "file": path.name, "ok": False, "error": None}
            try:
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)["payload"]
                bucket.append(parser(payload, cid, path.name))
                entry["ok"] = True
            except FileNotFoundError:
                entry["error"] = "file not found (extraction failed?)"
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                entry["error"] = f"unreadable: {exc}"
            if entry["error"]:
                logger.warning("Skipping %s/%s: %s", source, cid, entry["error"])
            availability.append(entry)

    weather = pd.concat(weather_frames, ignore_index=True) if weather_frames else _empty_weather()
    aq = pd.concat(aq_frames, ignore_index=True) if aq_frames else _empty_aq()
    return weather, aq, availability


def _empty_weather() -> pd.DataFrame:
    return pd.DataFrame(columns=["city_id", "date", *WEATHER_MEASURES, "source_file"])


def _empty_aq() -> pd.DataFrame:
    return pd.DataFrame(columns=["city_id", "timestamp", "pm25", "pm10", "source_file"])


# ---------------------------------------------------------------------------
# 2. Clean
# ---------------------------------------------------------------------------
def null_out_of_range(df: pd.DataFrame, ranges: dict[str, tuple]) -> pd.DataFrame:
    """Replace physically impossible values with NaN (never silently clip them)."""
    df = df.copy()
    for col, (lo, hi) in ranges.items():
        if col in df.columns:
            bad = df[col].notna() & ~df[col].between(lo, hi)
            df.loc[bad, col] = np.nan
    return df


def clean_weather(weather: pd.DataFrame) -> pd.DataFrame:
    """Drop exact duplicate city-days (keep first), keep only in-scope dates, null invalid values."""
    df = weather.drop_duplicates(subset=["city_id", "date"], keep="first")
    df = df[df["date"].between(config.START_DATE, config.END_DATE)]
    ranges = {k: v for k, v in config.VALID_RANGES.items() if k in WEATHER_MEASURES}
    return null_out_of_range(df, ranges).reset_index(drop=True)


AQ_HOURLY_RANGES = {"pm25": config.VALID_RANGES["pm25_mean"], "pm10": config.VALID_RANGES["pm10_mean"]}


def clean_air_quality(aq: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate city-hours and null invalid hourly readings."""
    df = aq.drop_duplicates(subset=["city_id", "timestamp"], keep="first")
    return null_out_of_range(df, AQ_HOURLY_RANGES).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 3. Aggregate air quality hourly -> daily
# ---------------------------------------------------------------------------
def aggregate_air_quality(aq_hourly: pd.DataFrame) -> pd.DataFrame:
    """One row per city-day. A day is 'complete' only with >= MIN_AQ_HOURS_PER_DAY valid hours."""
    df = aq_hourly.copy()
    df["date"] = df["timestamp"].dt.normalize()
    df["both_valid"] = df["pm25"].notna() & df["pm10"].notna()

    daily = (
        df.groupby(["city_id", "date"], as_index=False)
        .agg(
            pm25_mean=("pm25", "mean"),
            pm25_max=("pm25", "max"),
            pm10_mean=("pm10", "mean"),
            pm10_max=("pm10", "max"),
            hours_available=("both_valid", "sum"),
            source_file=("source_file", "first"),
        )
    )
    daily["hours_available"] = daily["hours_available"].astype(int)
    daily["is_complete_day"] = daily["hours_available"] >= config.MIN_AQ_HOURS_PER_DAY
    daily = daily[daily["date"].between(config.START_DATE, config.END_DATE)]
    return daily.round({"pm25_mean": 1, "pm25_max": 1, "pm10_mean": 1, "pm10_max": 1}).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. Dimensions
# ---------------------------------------------------------------------------
def build_dim_city() -> pd.DataFrame:
    return pd.DataFrame(config.CITIES)[["city_id", "city", "state", "region", "latitude", "longitude"]]


def build_dim_date(start: str = config.START_DATE, end: str = config.END_DATE) -> pd.DataFrame:
    dates = pd.date_range(start, end, freq="D")
    return pd.DataFrame({
        "date": dates,
        "year": dates.year,
        "month": dates.month,
        "month_name": dates.strftime("%b"),
        "year_month": dates.strftime("%Y-%m"),
        "quarter": "Q" + dates.quarter.astype(str),
        "season": dates.month.map(SEASONS),
        "day_of_week": dates.strftime("%a"),
        "is_weekend": dates.dayofweek >= 5,
    })


# ---------------------------------------------------------------------------
# 5. Integrated daily fact
# ---------------------------------------------------------------------------
def _flag(condition: pd.Series, known: pd.Series) -> pd.Series:
    """Boolean flag that is NULL (unknown) where the input data is missing."""
    flag = condition.astype("boolean")
    flag[~known] = pd.NA
    return flag


def add_risk_flags(df: pd.DataFrame,
                   heat_c: float = config.HEAT_DAY_TEMP_MAX_C,
                   rain_mm: float = config.HEAVY_RAIN_MM,
                   pm25_limit: float = config.PM25_LIMIT_UGM3,
                   pm10_limit: float = config.PM10_LIMIT_UGM3,
                   include: tuple[str, ...] = ("is_heat_day", "is_heavy_rain_day", "is_poor_air_day"),
                   ) -> pd.DataFrame:
    """Add the risk flags. Thresholds default to config, but the dashboard can pass its own
    (slider values) so the ETL and the app always share one definition of each flag.

    `include` decides which flags count towards is_disruption_day. All three flags are
    always computed, so excluding a risk never loses data.
    """
    df = df.copy()
    df["is_heat_day"] = _flag(df["temp_max"] >= heat_c, df["temp_max"].notna())
    df["is_heavy_rain_day"] = _flag(df["precipitation_mm"] >= rain_mm, df["precipitation_mm"].notna())
    poor_air = (df["pm25_mean"] > pm25_limit) | (df["pm10_mean"] > pm10_limit)
    df["is_poor_air_day"] = _flag(poor_air, df["has_air_quality"])
    disruption = pd.Series(False, index=df.index)
    for col in include:
        disruption = disruption | df[col].fillna(False).astype(bool)
    df["is_disruption_day"] = disruption
    return df


def build_fact_city_daily(dim_city: pd.DataFrame, dim_date: pd.DataFrame,
                          weather: pd.DataFrame, aq_daily: pd.DataFrame) -> pd.DataFrame:
    """Calendar spine (every city x every date) LEFT JOIN weather and air quality.

    Using a spine instead of a plain outer join means a day missing from BOTH sources
    still shows up as a row with has_weather = has_air_quality = False.
    """
    spine = dim_city[["city_id"]].merge(dim_date[["date"]], how="cross")

    wx = weather[["city_id", "date", *WEATHER_MEASURES]].assign(has_weather=True)
    aq = aq_daily[["city_id", "date", "pm25_mean", "pm25_max", "pm10_mean", "pm10_max",
                   "hours_available", "is_complete_day"]]

    fact = spine.merge(wx, on=["city_id", "date"], how="left").merge(aq, on=["city_id", "date"], how="left")
    fact["has_weather"] = fact["has_weather"].eq(True)
    fact["hours_available"] = fact["hours_available"].fillna(0).astype(int)
    fact["is_complete_day"] = fact["is_complete_day"].eq(True)
    fact["has_air_quality"] = fact["is_complete_day"]
    return add_risk_flags(fact).sort_values(["city_id", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 6. Monthly mart
# ---------------------------------------------------------------------------
def build_mart_city_month(fact: pd.DataFrame, dim_city: pd.DataFrame, dim_date: pd.DataFrame) -> pd.DataFrame:
    df = fact.merge(dim_date[["date", "year_month"]], on="date", how="left")
    df["pm25_complete"] = df["pm25_mean"].where(df["has_air_quality"])

    mart = (
        df.groupby(["city_id", "year_month"], as_index=False)
        .agg(
            days_in_month=("date", "count"),
            days_observed=("has_weather", "sum"),
            heat_days=("is_heat_day", lambda s: int(s.fillna(False).sum())),
            heavy_rain_days=("is_heavy_rain_day", lambda s: int(s.fillna(False).sum())),
            poor_air_days=("is_poor_air_day", lambda s: int(s.fillna(False).sum())),
            disruption_days=("is_disruption_day", "sum"),
            avg_temp_max=("temp_max", "mean"),
            max_temp_max=("temp_max", "max"),
            total_precipitation_mm=("precipitation_mm", lambda s: s.sum(min_count=1)),
            avg_pm25=("pm25_complete", "mean"),
            aq_days=("has_air_quality", "sum"),
        )
    )
    mart["disruption_rate"] = np.where(
        mart["days_observed"] > 0, mart["disruption_days"] / mart["days_observed"].replace(0, np.nan), np.nan
    )
    mart["aq_coverage_pct"] = 100 * mart["aq_days"] / mart["days_in_month"]
    mart = mart.drop(columns="aq_days")
    mart = mart.merge(dim_city[["city_id", "city", "region"]], on="city_id", how="left")

    cols = ["city_id", "city", "region", "year_month", "days_in_month", "days_observed",
            "heat_days", "heavy_rain_days", "poor_air_days", "disruption_days", "disruption_rate",
            "avg_temp_max", "max_temp_max", "total_precipitation_mm", "avg_pm25", "aq_coverage_pct"]
    return mart[cols].round({"disruption_rate": 3, "avg_temp_max": 1, "max_temp_max": 1,
                             "total_precipitation_mm": 1, "avg_pm25": 1, "aq_coverage_pct": 1})


def stamp(df: pd.DataFrame) -> pd.DataFrame:
    """Add the load timestamp (lineage)."""
    return df.assign(loaded_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
