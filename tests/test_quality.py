"""Tests that each data-quality check catches the problem it is designed for."""
import pandas as pd

from src import config, quality, transform


def by_id(results, check_id):
    return next(r for r in results if r["check_id"] == check_id)


def clean_weather_for_one_city(days=5):
    dates = pd.date_range(config.START_DATE, periods=days)
    return pd.DataFrame({
        "city_id": "DEL", "date": dates, "temp_max": 35.0, "temp_min": 25.0, "temp_mean": 30.0,
        "precipitation_mm": 0.0, "wind_max_kmh": 10.0, "source_file": "f",
    })


def test_status_rules():
    assert quality._result("X", "t", "n", "c", quality.ERROR, 10, 0)["status"] == "PASS"
    assert quality._result("X", "t", "n", "c", quality.WARN, 10, 3)["status"] == "WARN"
    assert quality._result("X", "t", "n", "c", quality.ERROR, 10, 3)["status"] == "FAIL"


def test_missing_raw_file_fails_availability():
    availability = [{"city_id": "DEL", "source": "weather", "ok": True, "error": None},
                    {"city_id": "MUM", "source": "air_quality", "ok": False, "error": "file not found"}]
    r = quality.check_availability(availability)[0]
    assert r["status"] == "FAIL" and r["rows_affected"] == 1 and "MUM" in r["details"]


def test_duplicate_weather_rows_detected():
    wx = clean_weather_for_one_city()
    wx = pd.concat([wx, wx.iloc[[0]]], ignore_index=True)
    r = by_id(quality.check_weather_source(wx, wx["date"].drop_duplicates()), "DQ_WX_01")
    assert r["rows_affected"] == 1


def test_missing_days_detected():
    wx = clean_weather_for_one_city(days=5).drop(index=2)
    expected = pd.date_range(config.START_DATE, periods=5)
    r = by_id(quality.check_weather_source(wx, expected), "DQ_WX_02")
    assert "DEL: 1" in r["details"]


def test_out_of_range_temperature_detected():
    wx = clean_weather_for_one_city()
    wx.loc[0, "temp_max"] = 99.0
    r = by_id(quality.check_weather_source(wx, wx["date"]), "DQ_WX_04")
    assert r["rows_affected"] == 1 and r["status"] == "WARN"


def test_min_greater_than_max_detected():
    wx = clean_weather_for_one_city()
    wx.loc[0, "temp_min"] = 50.0
    r = by_id(quality.check_weather_source(wx, wx["date"]), "DQ_WX_05")
    assert r["rows_affected"] == 1


def test_pm25_above_pm10_detected():
    ts = pd.date_range(config.START_DATE, periods=3, freq="h")
    aq = pd.DataFrame({"city_id": "DEL", "timestamp": ts, "pm25": [10.0, 90.0, 20.0], "pm10": [20.0, 50.0, 40.0]})
    r = by_id(quality.check_air_quality_source(aq), "DQ_AQ_04")
    assert r["rows_affected"] == 1


def _small_model():
    dim_city = transform.build_dim_city().head(1)
    dim_date = transform.build_dim_date(config.START_DATE, "2024-10-05")
    wx = clean_weather_for_one_city(days=5)
    ts = pd.date_range(config.START_DATE, periods=5 * 24, freq="h")
    aq_daily = transform.aggregate_air_quality(
        pd.DataFrame({"city_id": "DEL", "timestamp": ts, "pm25": 30.0, "pm10": 60.0, "source_file": "f"}))
    fact = transform.build_fact_city_daily(dim_city, dim_date, wx, aq_daily)
    mart = transform.build_mart_city_month(fact, dim_city, dim_date)
    return dim_city, dim_date, aq_daily, fact, mart


def test_clean_model_passes_every_check():
    results = quality.check_model(*_small_model())
    assert all(r["status"] == "PASS" for r in results), [r for r in results if r["status"] != "PASS"]


def test_duplicate_fact_key_fails():
    dim_city, dim_date, aq_daily, fact, mart = _small_model()
    fact = pd.concat([fact, fact.iloc[[0]]], ignore_index=True)
    assert by_id(quality.check_model(dim_city, dim_date, aq_daily, fact, mart), "DQ_FC_01")["status"] == "FAIL"


def test_orphan_city_fails_referential_integrity():
    dim_city, dim_date, aq_daily, fact, mart = _small_model()
    fact.loc[0, "city_id"] = "XXX"
    assert by_id(quality.check_model(dim_city, dim_date, aq_daily, fact, mart), "DQ_FC_02")["status"] == "FAIL"


def test_mart_that_does_not_reconcile_fails():
    dim_city, dim_date, aq_daily, fact, mart = _small_model()
    mart.loc[0, "disruption_days"] += 1
    assert by_id(quality.check_model(dim_city, dim_date, aq_daily, fact, mart), "DQ_MART_01")["status"] == "FAIL"
