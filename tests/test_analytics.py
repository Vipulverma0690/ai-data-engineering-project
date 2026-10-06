"""Tests for the dashboard's analytics layer (filters, user thresholds, KPIs, insights)."""
import pandas as pd

from src import analytics, config, transform


def small_fact(temps=(41.0, 30.0, 30.0), rain=(0.0, 70.0, 0.0)):
    dim_city = transform.build_dim_city().head(1)
    dim_date = transform.build_dim_date(config.START_DATE, "2024-10-03")
    wx = pd.DataFrame({"city_id": "DEL", "date": dim_date["date"], "temp_max": temps, "temp_min": 20.0,
                       "temp_mean": 25.0, "precipitation_mm": rain, "wind_max_kmh": 10.0})
    ts = pd.date_range(config.START_DATE, periods=3 * 24, freq="h")
    aq = transform.aggregate_air_quality(
        pd.DataFrame({"city_id": "DEL", "timestamp": ts, "pm25": 20.0, "pm10": 40.0, "source_file": "f"}))
    return dim_city, dim_date, transform.build_fact_city_daily(dim_city, dim_date, wx, aq)


def test_user_thresholds_change_the_flags():
    dim_city, dim_date, fact = small_fact()
    months = ("2024-10", "2024-10")
    strict = analytics.apply_filters_and_thresholds(fact, dim_date, ["DEL"], months, 40.0, 64.5, 60, 100)
    loose = analytics.apply_filters_and_thresholds(fact, dim_date, ["DEL"], months, 29.0, 64.5, 60, 100)
    assert int(strict["is_heat_day"].sum()) == 1
    assert int(loose["is_heat_day"].sum()) == 3


def test_city_filter_excludes_other_cities():
    dim_city, dim_date, fact = small_fact()
    out = analytics.apply_filters_and_thresholds(fact, dim_date, ["MUM"], ("2024-10", "2024-10"),
                                                 40, 64.5, 60, 100)
    assert out.empty


def test_kpis_count_disruption_days():
    dim_city, dim_date, fact = small_fact()
    df = analytics.apply_filters_and_thresholds(fact, dim_date, ["DEL"], ("2024-10", "2024-10"),
                                                40, 64.5, 60, 100)
    k = analytics.kpis(df, dim_city)
    assert k["disruption_days"] == 2          # day 1 heat, day 2 rain
    assert k["worst_city"] == "Delhi"
    assert abs(k["disruption_rate"] - 2 / 3) < 1e-9


def test_monthly_view_reuses_etl_mart_logic():
    dim_city, dim_date, fact = small_fact()
    df = analytics.apply_filters_and_thresholds(fact, dim_date, ["DEL"], ("2024-10", "2024-10"),
                                                40, 64.5, 60, 100)
    m = analytics.monthly(df, dim_city, dim_date)
    assert m.loc[0, "heat_days"] == 1 and m.loc[0, "heavy_rain_days"] == 1


def test_insights_are_generated_and_handle_empty_selection():
    dim_city, dim_date, fact = small_fact()
    df = analytics.apply_filters_and_thresholds(fact, dim_date, ["DEL"], ("2024-10", "2024-10"),
                                                40, 64.5, 60, 100)
    assert len(analytics.insights(df, dim_city)) >= 2
    assert analytics.insights(df.iloc[0:0], dim_city) == ["No data for the current selection."]


def test_excluding_a_risk_removes_it_from_disruption_but_keeps_the_flag():
    dim_city, dim_date, fact = small_fact()          # day 1 heat, day 2 heavy rain
    months = ("2024-10", "2024-10")
    weather_only = analytics.apply_filters_and_thresholds(
        fact, dim_date, ["DEL"], months, 40, 64.5, 60, 100, include=("is_heat_day",))
    assert int(weather_only["is_disruption_day"].sum()) == 1      # only the heat day counts
    assert int(weather_only["is_heavy_rain_day"].sum()) == 1      # rain flag still computed
