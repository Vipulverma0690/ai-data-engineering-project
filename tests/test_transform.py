"""Unit tests for the transformation logic, using small hand-made data (no API, no disk)."""
import pandas as pd
import pytest

from src import config, transform


def weather_payload(days=3, **overrides):
    dates = pd.date_range(config.START_DATE, periods=days).strftime("%Y-%m-%d").tolist()
    daily = {
        "time": dates,
        "temperature_2m_max": [35.0] * days,
        "temperature_2m_min": [25.0] * days,
        "temperature_2m_mean": [30.0] * days,
        "precipitation_sum": [0.0] * days,
        "wind_speed_10m_max": [10.0] * days,
    }
    daily.update(overrides)
    return {"daily": daily}


def aq_hourly(city_id="DEL", day=config.START_DATE, hours=24, pm25=50.0, pm10=80.0):
    ts = pd.date_range(day, periods=hours, freq="h")
    return pd.DataFrame({"city_id": city_id, "timestamp": ts, "pm25": pm25, "pm10": pm10, "source_file": "f"})


# ---------- parsing ----------
def test_parse_weather_renames_columns_and_keeps_all_rows():
    df = transform.parse_weather(weather_payload(), "DEL", "f.json")
    assert len(df) == 3
    assert {"city_id", "date", "temp_max", "precipitation_mm", "wind_max_kmh"} <= set(df.columns)
    assert pd.api.types.is_datetime64_any_dtype(df["date"])


def test_parse_weather_rejects_payload_without_daily_block():
    with pytest.raises(ValueError):
        transform.parse_weather({"error": True}, "DEL", "f.json")


def test_parse_weather_missing_variable_becomes_null_column():
    payload = weather_payload()
    del payload["daily"]["wind_speed_10m_max"]
    df = transform.parse_weather(payload, "DEL", "f.json")
    assert df["wind_max_kmh"].isna().all()


# ---------- cleaning ----------
def test_out_of_range_values_are_nulled_not_clipped():
    df = pd.DataFrame({"temp_max": [30.0, 99.0, -50.0]})
    out = transform.null_out_of_range(df, {"temp_max": (-10, 55)})
    assert out["temp_max"].tolist()[0] == 30.0
    assert out["temp_max"].iloc[1:].isna().all()


def test_clean_weather_drops_duplicate_city_days():
    df = transform.parse_weather(weather_payload(), "DEL", "f.json")
    doubled = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    assert len(transform.clean_weather(doubled)) == 3


# ---------- air-quality aggregation ----------
def test_full_day_of_hours_is_complete():
    daily = transform.aggregate_air_quality(aq_hourly(hours=24))
    assert daily.loc[0, "hours_available"] == 24
    assert bool(daily.loc[0, "is_complete_day"])
    assert daily.loc[0, "pm25_mean"] == 50.0


def test_day_with_too_few_hours_is_incomplete():
    daily = transform.aggregate_air_quality(aq_hourly(hours=config.MIN_AQ_HOURS_PER_DAY - 1))
    assert not bool(daily.loc[0, "is_complete_day"])


# ---------- dimensions ----------
def test_dim_date_covers_full_range_with_imd_seasons():
    d = transform.build_dim_date()
    assert len(d) == 730
    assert d.loc[d["date"] == "2025-07-15", "season"].item() == "Monsoon"
    assert d.loc[d["date"] == "2025-01-15", "season"].item() == "Winter"


# ---------- risk flags ----------
def _fact_row(**kw):
    base = dict(temp_max=35.0, precipitation_mm=0.0, pm25_mean=20.0, pm10_mean=40.0, has_air_quality=True)
    base.update(kw)
    return pd.DataFrame([base])


@pytest.mark.parametrize("temp, expected", [(40.0, True), (39.9, False)])
def test_heat_flag_threshold_is_inclusive(temp, expected):
    assert bool(transform.add_risk_flags(_fact_row(temp_max=temp))["is_heat_day"].iloc[0]) is expected


def test_heavy_rain_flag_uses_imd_threshold():
    flags = transform.add_risk_flags(_fact_row(precipitation_mm=64.5))
    assert bool(flags["is_heavy_rain_day"].iloc[0])


def test_missing_temperature_gives_unknown_flag_not_false():
    flags = transform.add_risk_flags(_fact_row(temp_max=float("nan")))
    assert pd.isna(flags["is_heat_day"].iloc[0])


def test_poor_air_flag_is_unknown_when_air_quality_day_incomplete():
    flags = transform.add_risk_flags(_fact_row(pm25_mean=200.0, has_air_quality=False))
    assert pd.isna(flags["is_poor_air_day"].iloc[0])
    assert not flags["is_disruption_day"].iloc[0]


def test_any_flag_makes_a_disruption_day():
    flags = transform.add_risk_flags(_fact_row(pm10_mean=150.0))
    assert flags["is_disruption_day"].iloc[0]


# ---------- integrated fact ----------
def test_fact_spine_keeps_days_missing_from_sources():
    dim_city = transform.build_dim_city().head(1)               # DEL only
    dim_date = transform.build_dim_date(config.START_DATE, "2024-10-03")
    weather = transform.parse_weather(weather_payload(days=2), "DEL", "f.json")   # day 3 missing
    aq = transform.aggregate_air_quality(aq_hourly(hours=24))                     # day 1 only
    fact = transform.build_fact_city_daily(dim_city, dim_date, weather, aq)

    assert len(fact) == 3
    assert fact["has_weather"].tolist() == [True, True, False]
    assert fact["has_air_quality"].tolist() == [True, False, False]


def test_mart_counts_reconcile_to_fact():
    dim_city = transform.build_dim_city().head(1)
    dim_date = transform.build_dim_date(config.START_DATE, "2024-10-03")
    weather = transform.parse_weather(
        weather_payload(days=3, temperature_2m_max=[41.0, 42.0, 30.0]), "DEL", "f.json")
    aq = transform.aggregate_air_quality(aq_hourly(hours=24))
    fact = transform.build_fact_city_daily(dim_city, dim_date, weather, aq)
    mart = transform.build_mart_city_month(fact, dim_city, dim_date)

    assert mart.loc[0, "heat_days"] == 2
    assert mart.loc[0, "days_observed"] == 3
    assert mart.loc[0, "disruption_rate"] == pytest.approx(2 / 3, abs=0.001)
