"""Analytics layer used by the dashboard: filtering, KPIs and plain-English insights.

Kept separate from app.py so the business logic can be unit-tested without Streamlit.
"""
from __future__ import annotations

import pandas as pd

from src import transform

RISKS = {  # flag column -> label used in the dashboard
    "is_heat_day": "Heat",
    "is_heavy_rain_day": "Heavy rain",
    "is_poor_air_day": "Poor air",
}


def apply_filters_and_thresholds(fact: pd.DataFrame, dim_date: pd.DataFrame, cities: list[str],
                                 months: tuple[str, str], heat_c: float, rain_mm: float,
                                 pm25_limit: float, pm10_limit: float,
                                 include: tuple[str, ...] = tuple(RISKS)) -> pd.DataFrame:
    """Filter the daily fact and recompute flags with the user's thresholds and chosen risks."""
    df = fact.merge(dim_date[["date", "year_month", "month_name", "season", "year"]], on="date", how="left")
    df = df[df["city_id"].isin(cities) & df["year_month"].between(*months)]
    return transform.add_risk_flags(df, heat_c, rain_mm, pm25_limit, pm10_limit, include)


def monthly(df: pd.DataFrame, dim_city: pd.DataFrame, dim_date: pd.DataFrame) -> pd.DataFrame:
    """Re-aggregate to city x month with the SAME function the ETL uses for the mart."""
    base = df.drop(columns=["year_month", "month_name", "season", "year"], errors="ignore")
    return transform.build_mart_city_month(base, dim_city, dim_date)


def kpis(df: pd.DataFrame, dim_city: pd.DataFrame) -> dict:
    observed = int(df["has_weather"].sum())
    disruption = int(df["is_disruption_day"].sum())
    by_city = (df.groupby("city_id")
               .agg(d=("is_disruption_day", "sum"), n=("has_weather", "sum"))
               .assign(rate=lambda x: x["d"] / x["n"].where(x["n"] > 0)))
    worst_id = by_city["rate"].idxmax() if by_city["rate"].notna().any() else None
    names = dim_city.set_index("city_id")["city"]
    return {
        "city_days": observed,
        "disruption_days": disruption,
        "disruption_rate": disruption / observed if observed else float("nan"),
        "heat_days": int(df["is_heat_day"].fillna(False).sum()),
        "rain_days": int(df["is_heavy_rain_day"].fillna(False).sum()),
        "air_days": int(df["is_poor_air_day"].fillna(False).sum()),
        "worst_city": names.get(worst_id, "-") if worst_id else "-",
        "worst_city_rate": float(by_city.loc[worst_id, "rate"]) if worst_id else float("nan"),
        "aq_coverage": float(df["has_air_quality"].mean()) if len(df) else float("nan"),
    }


def insights(df: pd.DataFrame, dim_city: pd.DataFrame,
             include: tuple[str, ...] = tuple(RISKS)) -> list[str]:
    """A few data-driven sentences for a business reader. Every number is computed, never hard-coded."""
    out: list[str] = []
    if df.empty or not df["has_weather"].any():
        return ["No data for the current selection."]
    names = dim_city.set_index("city_id")["city"]

    # 1. Which risk drives disruption?
    risks = {col: label for col, label in RISKS.items() if col in include}
    counts = {label: int(df[col].fillna(False).sum()) for col, label in risks.items()}
    top_risk = max(counts, key=counts.get)
    total_flags = sum(counts.values())
    if total_flags and len(counts) > 1:
        out.append(f"**{top_risk}** is the main driver: {counts[top_risk]:,} of {total_flags:,} "
                   f"risk-days ({counts[top_risk] / total_flags:.0%}) in the selection.")

    # 2. Most exposed city for each risk
    for col, label in risks.items():
        per_city = df.groupby("city_id")[col].apply(lambda s: int(s.fillna(False).sum()))
        if per_city.max() > 0:
            cid = per_city.idxmax()
            share = per_city.max() / per_city.sum()
            out.append(f"{label}: **{names[cid]}** accounts for {share:.0%} of all {label.lower()} days "
                       f"({per_city.max():,} days).")

    # 3. Seasonality: when do disruptions concentrate?
    by_season = df.groupby("season")["is_disruption_day"].mean().dropna()
    if len(by_season) > 1 and by_season.max() > 0:
        hi, lo = by_season.idxmax(), by_season.idxmin()
        out.append(f"Disruption risk peaks in **{hi}** ({by_season[hi]:.0%} of city-days) and is lowest in "
                   f"**{lo}** ({by_season[lo]:.0%}), so plan buffers ahead of {hi.lower()}.")

    # 4. Year-over-year trend (Oct-Sep operating years)
    fy = df["date"].dt.year.where(df["date"].dt.month < 10, df["date"].dt.year + 1)
    yearly = df.assign(fy=fy).groupby("fy")["is_disruption_day"].sum()
    if len(yearly) == 2 and yearly.iloc[0] > 0:
        change = yearly.iloc[1] / yearly.iloc[0] - 1
        direction = "up" if change > 0 else "down"
        out.append(f"Year-on-year, disruption days went **{direction} {abs(change):.0%}** "
                   f"({yearly.iloc[0]:,} to {yearly.iloc[1]:,}, Oct-Sep years).")
    return out
