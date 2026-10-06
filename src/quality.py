"""Data-quality checks.

Each check returns one result row:
    check_id, table_name, check_name, check_type, severity, status,
    rows_checked, rows_affected, details

Status rules:
    rows_affected == 0          -> PASS
    rows_affected > 0 and WARN  -> WARN   (data is usable; issue is visible in the dashboard)
    rows_affected > 0 and ERROR -> FAIL   (output should not be trusted)

Source checks run on the data *before* cleaning, so the report shows what the
API actually sent. Model checks run on the final tables to prove the ETL is correct.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src import config

WARN, ERROR = "WARN", "ERROR"


def _result(check_id, table, name, check_type, severity, rows_checked, rows_affected, details=""):
    rows_affected = int(rows_affected)
    if rows_affected == 0:
        status = "PASS"
    else:
        status = "FAIL" if severity == ERROR else "WARN"
    return {
        "check_id": check_id, "table_name": table, "check_name": name, "check_type": check_type,
        "severity": severity, "status": status, "rows_checked": int(rows_checked),
        "rows_affected": rows_affected, "details": details,
    }


def _out_of_range(df: pd.DataFrame, ranges: dict) -> tuple[int, str]:
    total, parts = 0, []
    for col, (lo, hi) in ranges.items():
        if col not in df.columns:
            continue
        bad = df[col].notna() & ~df[col].between(lo, hi)
        n = int(bad.sum())
        if n:
            total += n
            parts.append(f"{col}: {n} outside [{lo}, {hi}]")
    return total, "; ".join(parts)


# ---------------------------------------------------------------------------
# Source checks (before cleaning)
# ---------------------------------------------------------------------------
def check_availability(availability: list[dict]) -> list[dict]:
    failed = [a for a in availability if not a["ok"]]
    details = "; ".join(f"{a['source']}/{a['city_id']}: {a['error']}" for a in failed)
    return [_result("DQ_SRC_01", "raw", "Raw file present and readable for every city x source",
                    "availability", ERROR, len(availability), len(failed), details)]


def check_weather_source(weather: pd.DataFrame, expected_dates: pd.DatetimeIndex) -> list[dict]:
    n = len(weather)
    results = []

    # Uniqueness
    dups = weather.duplicated(subset=["city_id", "date"], keep="first").sum()
    results.append(_result("DQ_WX_01", "weather (raw)", "Duplicate city-date rows", "uniqueness",
                           WARN, n, dups, "duplicates are dropped (first kept)" if dups else ""))

    # Completeness: missing calendar days per city
    missing_total, parts = 0, []
    for city in config.CITIES:
        got = set(weather.loc[weather["city_id"] == city["city_id"], "date"])
        missing = len(set(expected_dates) - got)
        if missing:
            missing_total += missing
            parts.append(f"{city['city_id']}: {missing}")
    results.append(_result("DQ_WX_02", "weather (raw)", "Missing days in expected date range",
                           "completeness", WARN, len(expected_dates) * len(config.CITIES),
                           missing_total, "; ".join(parts)))

    # Nulls in measures
    measures = ["temp_max", "temp_min", "temp_mean", "precipitation_mm", "wind_max_kmh"]
    null_counts = {c: int(weather[c].isna().sum()) for c in measures if c in weather}
    null_rows = int(weather[measures].isna().any(axis=1).sum()) if n else 0
    results.append(_result("DQ_WX_03", "weather (raw)", "Null values in weather measures",
                           "completeness", WARN, n, null_rows,
                           ", ".join(f"{c}: {v}" for c, v in null_counts.items() if v)))

    # Validity: physically plausible ranges
    ranges = {k: v for k, v in config.VALID_RANGES.items() if k in measures}
    bad, details = _out_of_range(weather, ranges)
    results.append(_result("DQ_WX_04", "weather (raw)", "Values outside plausible range",
                           "validity", WARN, n, bad, (details + " -> set to null") if bad else ""))

    # Consistency: min <= mean <= max
    t = weather.dropna(subset=["temp_min", "temp_mean", "temp_max"])
    inconsistent = ((t["temp_min"] > t["temp_mean"] + 0.05) | (t["temp_mean"] > t["temp_max"] + 0.05)).sum()
    results.append(_result("DQ_WX_05", "weather (raw)", "temp_min <= temp_mean <= temp_max",
                           "consistency", WARN, len(t), inconsistent))
    return results


def check_air_quality_source(aq_hourly: pd.DataFrame) -> list[dict]:
    n = len(aq_hourly)
    results = []

    dups = aq_hourly.duplicated(subset=["city_id", "timestamp"], keep="first").sum()
    results.append(_result("DQ_AQ_01", "air_quality (raw hourly)", "Duplicate city-hour rows",
                           "uniqueness", WARN, n, dups, "duplicates are dropped (first kept)" if dups else ""))

    nulls = aq_hourly[["pm25", "pm10"]].isna().any(axis=1).sum() if n else 0
    results.append(_result("DQ_AQ_02", "air_quality (raw hourly)", "Null PM2.5 / PM10 readings",
                           "completeness", WARN, n, nulls))

    bad, details = _out_of_range(aq_hourly, {"pm25": config.VALID_RANGES["pm25_mean"],
                                             "pm10": config.VALID_RANGES["pm10_mean"]})
    results.append(_result("DQ_AQ_03", "air_quality (raw hourly)", "Readings outside plausible range",
                           "validity", WARN, n, bad, (details + " -> set to null") if bad else ""))

    # Physics: PM2.5 is a subset of PM10, so PM2.5 should not exceed PM10
    both = aq_hourly.dropna(subset=["pm25", "pm10"])
    violations = (both["pm25"] > both["pm10"] * 1.05).sum()
    results.append(_result("DQ_AQ_04", "air_quality (raw hourly)", "PM2.5 <= PM10 (PM2.5 is a subset of PM10)",
                           "consistency", WARN, len(both), violations))
    return results


# ---------------------------------------------------------------------------
# Model checks (after transformation)
# ---------------------------------------------------------------------------
def check_model(dim_city, dim_date, aq_daily, fact, mart) -> list[dict]:
    results = []
    n = len(fact)

    incomplete = (~aq_daily["is_complete_day"]).sum()
    results.append(_result("DQ_AQ_05", "fact_air_quality_daily",
                           f"Days with < {config.MIN_AQ_HOURS_PER_DAY} valid hours (excluded from air metrics)",
                           "completeness", WARN, len(aq_daily), incomplete))

    dup_pk = fact.duplicated(subset=["city_id", "date"]).sum()
    results.append(_result("DQ_FC_01", "fact_city_daily", "Primary key (city_id, date) is unique",
                           "uniqueness", ERROR, n, dup_pk))

    orphans = (~fact["city_id"].isin(dim_city["city_id"])).sum()
    results.append(_result("DQ_FC_02", "fact_city_daily", "Every city_id exists in dim_city",
                           "referential integrity", ERROR, n, orphans))

    bad_dates = (~fact["date"].isin(dim_date["date"])).sum()
    results.append(_result("DQ_FC_03", "fact_city_daily", "Every date exists in dim_date",
                           "referential integrity", ERROR, n, bad_dates))

    expected = len(dim_city) * len(dim_date)
    results.append(_result("DQ_FC_04", "fact_city_daily", "Row count = cities x days",
                           "completeness", ERROR, expected, abs(expected - n), f"expected {expected}, got {n}"))

    wx_only = (fact["has_weather"] & ~fact["has_air_quality"]).sum()
    aq_only = (~fact["has_weather"] & fact["has_air_quality"]).sum()
    neither = (~fact["has_weather"] & ~fact["has_air_quality"]).sum()
    results.append(_result("DQ_FC_05", "fact_city_daily", "Days missing one or both sources",
                           "join coverage", WARN, n, wx_only + aq_only + neither,
                           f"weather only: {wx_only}, air quality only: {aq_only}, neither: {neither}"))

    # Reconciliation: mart totals must equal fact totals
    mismatches = []
    for mart_col, fact_col in [("heat_days", "is_heat_day"), ("heavy_rain_days", "is_heavy_rain_day"),
                               ("poor_air_days", "is_poor_air_day"), ("disruption_days", "is_disruption_day")]:
        m, f = int(mart[mart_col].sum()), int(fact[fact_col].fillna(False).sum())
        if m != f:
            mismatches.append(f"{mart_col}: mart {m} vs fact {f}")
    results.append(_result("DQ_MART_01", "mart_city_month", "Monthly totals reconcile to daily fact",
                           "consistency", ERROR, 4, len(mismatches), "; ".join(mismatches)))
    return results


def build_report(results: list[dict]) -> pd.DataFrame:
    report = pd.DataFrame(results)
    report.insert(0, "run_id", datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    return report
