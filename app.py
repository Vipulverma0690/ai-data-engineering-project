"""City Operations Risk Monitor - Streamlit dashboard.

Reads ONLY the processed outputs written by `python etl.py`; it never calls the API.
Run:  streamlit run app.py
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src import analytics, config, storage

st.set_page_config(page_title="City Operations Risk Monitor", page_icon="🌦️", layout="wide")

# ---------------------------------------------------------------------------
# Colours (validated colour-blind-safe categorical slots 1-3, one per risk type,
# always the same colour for the same risk; separate steps for light and dark themes)
# ---------------------------------------------------------------------------
DARK = getattr(getattr(st.context, "theme", None), "type", "light") == "dark"
RISK_COLORS = (
    {"Heavy rain": "#3987e5", "Heat": "#d95926", "Poor air": "#199e70"} if DARK
    else {"Heavy rain": "#2a78d6", "Heat": "#eb6834", "Poor air": "#1baf7a"}
)
SEQ_BLUE = (["#1a1a19", "#184f95", "#2a78d6", "#5598e7", "#9ec5f4"] if DARK
            else ["#f0efec", "#9ec5f4", "#3987e5", "#1c5cab", "#0d366b"])
SURFACE = "#1a1a19" if DARK else "#fcfcfb"
GRID = "#2c2c2a" if DARK else "#e1e0d9"
MUTED = "#898781"
THRESHOLD_LINE = "#c3c2b7" if DARK else "#52514e"
STATUS = {"PASS": "✅ PASS", "WARN": "⚠️ WARN", "FAIL": "❌ FAIL"}


def style(fig: go.Figure, height: int = 380, legend: bool = True) -> go.Figure:
    """Shared chart chrome: recessive grid, one axis, legend above the plot, hover on."""
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=40, b=10),
        font=dict(family='system-ui, -apple-system, "Segoe UI", sans-serif', size=13),
        hoverlabel=dict(font_size=13), showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title=None,
                    traceorder="normal"),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, zeroline=False, tickfont=dict(color=MUTED))
    return fig


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@st.cache_data
def load():
    tables = {name: storage.load_table(name) for name in
              ["dim_city", "dim_date", "fact_city_daily", "mart_city_month"]}
    dq = storage.load_dq_report() if storage.DQ_REPORT_PATH.exists() else pd.DataFrame()
    return tables, dq


try:
    tables, dq = load()
except (FileNotFoundError, OSError):
    st.error("Processed data not found. Run the pipeline first:\n\n```\npython etl.py\n```\n\n"
             "then refresh this page.")
    st.stop()

dim_city, dim_date, fact = tables["dim_city"], tables["dim_date"], tables["fact_city_daily"]
city_name = dim_city.set_index("city_id")["city"].to_dict()
city_id_by_name = {v: k for k, v in city_name.items()}
all_months = sorted(dim_date["year_month"].unique())

# ---------------------------------------------------------------------------
# Sidebar: filters and thresholds
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Filters")
    regions = st.multiselect("Region", sorted(dim_city["region"].unique()),
                             default=sorted(dim_city["region"].unique()))
    region_cities = dim_city[dim_city["region"].isin(regions)]["city"].tolist()
    chosen = st.multiselect("Cities", region_cities, default=region_cities)
    months = st.select_slider("Period", options=all_months, value=(all_months[0], all_months[-1]))

    st.header("Risks included")
    st.caption("Which risks count as a 'disruption day'. Untick Poor air to see weather-only disruption.")
    include = tuple(col for col, label in analytics.RISKS.items()
                    if st.checkbox(label, value=True, key=f"inc_{col}"))

    st.header("Risk thresholds")
    st.caption("Defaults are the official definitions. Adjust to test sensitivity.")
    heat_c = st.slider("Heat day: max temp ≥ (°C)", 35.0, 46.0, config.HEAT_DAY_TEMP_MAX_C, 0.5,
                       help="IMD heatwave criterion for the plains, simplified.")
    rain_mm = st.slider("Heavy rain: daily rain ≥ (mm)", 20.0, 120.0, config.HEAVY_RAIN_MM, 0.5,
                        help="IMD 'heavy rainfall' = 64.5 mm. Model data smooths local downpours, "
                             "so a lower value may better reflect operational disruption.")
    pm25 = st.slider("Poor air: PM2.5 daily mean > (µg/m³)", 15.0, 120.0, config.PM25_LIMIT_UGM3, 5.0,
                     help="India NAAQS 24-hour standard = 60. WHO guideline = 15.")
    pm10 = st.slider("Poor air: PM10 daily mean > (µg/m³)", 45.0, 200.0, config.PM10_LIMIT_UGM3, 5.0,
                     help="India NAAQS 24-hour standard = 100. WHO guideline = 45.")

if not chosen:
    st.warning("Select at least one city in the sidebar.")
    st.stop()
if not include:
    st.warning("Tick at least one risk under 'Risks included' in the sidebar.")
    st.stop()
SHOWN = [analytics.RISKS[c] for c in include]  # risk labels to draw, in fixed order

ids = [city_id_by_name[c] for c in chosen]
df = analytics.apply_filters_and_thresholds(fact, dim_date, ids, months, heat_c, rain_mm, pm25, pm10,
                                            include)
monthly = analytics.monthly(df, dim_city, dim_date)
k = analytics.kpis(df, dim_city)

# ---------------------------------------------------------------------------
# Header + KPIs
# ---------------------------------------------------------------------------
st.title("🌦️ City Operations Risk Monitor")
st.caption(f"Disruption risk ({', '.join(SHOWN).lower()}) across {len(ids)} Indian cities · "
           f"{months[0]} to {months[1]} · Source: Open-Meteo (historical weather + CAMS air quality)")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Disruption days", f"{k['disruption_days']:,}", help="City-days with at least one risk flag")
c2.metric("Disruption rate", f"{k['disruption_rate']:.0%}", help="Disruption days / city-days observed")
c3.metric("Most exposed city", k["worst_city"],
          help=f"Highest disruption rate in the selection: {k['worst_city_rate']:.0%} of its days")
risk_days = {r: n for r, n in {"Heat": k["heat_days"], "Heavy rain": k["rain_days"],
                               "Poor air": k["air_days"]}.items() if r in SHOWN}
top_risk = max(risk_days, key=risk_days.get)
c4.metric("Main risk driver", top_risk,
          help="Risk-days by type: " + " · ".join(f"{r} {n:,}" for r, n in risk_days.items())
               + ". One day can carry more than one risk.")
c5.metric("Air-quality coverage", f"{k['aq_coverage']:.0%}", help="Share of city-days with a complete "
          f"air-quality reading (≥ {config.MIN_AQ_HOURS_PER_DAY} of 24 hours)")

tab_overview, tab_trends, tab_city, tab_dq, tab_data = st.tabs(
    ["Overview", "Trends & seasonality", "City deep-dive", "Data quality", "Data explorer"])

# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
with tab_overview:
    st.subheader("Key insights")
    for line in analytics.insights(df, dim_city, include):
        st.markdown(f"- {line}")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Disruption rate by city and month")
        heat = monthly.pivot(index="city", columns="year_month", values="disruption_rate")
        order = monthly.groupby("city")["disruption_days"].sum().sort_values(ascending=False).index
        heat = heat.reindex(order)
        fig = go.Figure(go.Heatmap(
            z=heat.values * 100, x=heat.columns, y=heat.index, colorscale=SEQ_BLUE, zmin=0, zmax=100,
            xgap=2, ygap=2, colorbar=dict(title="% days", ticksuffix="%"),
            hovertemplate="<b>%{y}</b> · %{x}<br>Disruption rate: %{z:.0f}%<extra></extra>"))
        st.plotly_chart(style(fig, 360, legend=False), width="stretch")
        st.caption("Darker = more days with at least one risk. Cities sorted by total disruption days.")

    with right:
        st.subheader("Risk-days by city")
        per_city = (df.groupby("city_id")[list(analytics.RISKS)]
                    .agg(lambda s: int(s.fillna(False).sum()))
                    .rename(columns=analytics.RISKS, index=city_name))
        per_city = per_city.loc[per_city[SHOWN].sum(axis=1).sort_values().index]
        fig = go.Figure()
        for risk in SHOWN:
            fig.add_bar(y=per_city.index, x=per_city[risk], name=risk, orientation="h",
                        marker=dict(color=RISK_COLORS[risk], line=dict(color=SURFACE, width=2)),
                        hovertemplate=f"<b>%{{y}}</b><br>{risk}: %{{x:,}} days<extra></extra>")
        fig.update_layout(barmode="stack")
        st.plotly_chart(style(fig, 360), width="stretch")
        st.caption("A single day can count under more than one risk type.")

# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------
with tab_trends:
    st.subheader("Risk-days per month (all selected cities)")
    trend = (df.groupby("year_month")[list(analytics.RISKS)]
             .agg(lambda s: int(s.fillna(False).sum())).rename(columns=analytics.RISKS))
    fig = go.Figure()
    for risk in SHOWN:
        fig.add_scatter(x=trend.index, y=trend[risk], name=risk, mode="lines+markers",
                        line=dict(color=RISK_COLORS[risk], width=2), marker=dict(size=8),
                        hovertemplate=f"{risk}: %{{y:,}} days<extra></extra>")
    fig.update_layout(hovermode="x unified")
    st.plotly_chart(style(fig), width="stretch")

    st.subheader("Seasonal profile")
    season_order = ["Winter", "Pre-monsoon", "Monsoon", "Post-monsoon"]
    seas = (df.groupby("season")[list(analytics.RISKS)]
            .agg(lambda s: s.fillna(False).mean() * 100).rename(columns=analytics.RISKS)
            .reindex(season_order))
    fig = go.Figure()
    for risk in SHOWN:
        fig.add_bar(x=seas.index, y=seas[risk], name=risk,
                    marker=dict(color=RISK_COLORS[risk], line=dict(color=SURFACE, width=2)),
                    hovertemplate=f"%{{x}}<br>{risk}: %{{y:.1f}}% of city-days<extra></extra>")
    fig.update_layout(barmode="group")
    fig.update_yaxes(ticksuffix="%")
    st.plotly_chart(style(fig, 340), width="stretch")
    st.caption("IMD seasons: Winter Jan–Feb · Pre-monsoon Mar–May · Monsoon Jun–Sep · Post-monsoon Oct–Dec.")

# ---------------------------------------------------------------------------
# City deep-dive (separate charts per measure: never two y-axes on one chart)
# ---------------------------------------------------------------------------
with tab_city:
    city = st.selectbox("City", chosen)
    cdf = df[df["city_id"] == city_id_by_name[city]].sort_values("date")

    def daily_line(col, title, unit, threshold, color, label):
        fig = go.Figure(go.Scatter(x=cdf["date"], y=cdf[col], mode="lines", line=dict(color=color, width=2),
                                   name=title, hovertemplate=f"%{{x|%d %b %Y}}<br>{title}: %{{y:.1f}} {unit}"
                                                             "<extra></extra>"))
        fig.add_hline(y=threshold, line=dict(color=THRESHOLD_LINE, width=1, dash="dash"),
                      annotation_text=f"{label} {threshold:g} {unit}", annotation_position="top left",
                      annotation_font_color=MUTED)
        fig.update_layout(title=dict(text=title, font=dict(size=15)))
        return style(fig, 280, legend=False)

    st.plotly_chart(daily_line("temp_max", "Daily max temperature", "°C", heat_c, RISK_COLORS["Heat"],
                               "Heat threshold"), width="stretch")
    rain = go.Figure(go.Bar(x=cdf["date"], y=cdf["precipitation_mm"], marker_color=RISK_COLORS["Heavy rain"],
                            hovertemplate="%{x|%d %b %Y}<br>Rain: %{y:.1f} mm<extra></extra>"))
    rain.add_hline(y=rain_mm, line=dict(color=THRESHOLD_LINE, width=1, dash="dash"),
                   annotation_text=f"Heavy rain {rain_mm:g} mm", annotation_position="top left",
                   annotation_font_color=MUTED)
    rain.update_layout(title=dict(text="Daily rainfall", font=dict(size=15)))
    st.plotly_chart(style(rain, 280, legend=False), width="stretch")
    st.plotly_chart(daily_line("pm25_mean", "Daily mean PM2.5", "µg/m³", pm25, RISK_COLORS["Poor air"],
                               "Limit"), width="stretch")

# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------
with tab_dq:
    st.subheader("Data-quality report (latest ETL run)")
    if dq.empty:
        st.info("No data-quality report found. Run `python etl.py`.")
    else:
        counts = dq["status"].value_counts()
        a, b, c = st.columns(3)
        a.metric("✅ Passed", int(counts.get("PASS", 0)))
        b.metric("⚠️ Warnings", int(counts.get("WARN", 0)))
        c.metric("❌ Failed", int(counts.get("FAIL", 0)))
        show = dq.assign(status=dq["status"].map(STATUS), details=dq["details"].fillna(""))[
            ["status", "check_id", "check_name", "table_name", "check_type", "severity",
             "rows_checked", "rows_affected", "details"]]
        st.dataframe(show, width="stretch", hide_index=True)
        st.caption(f"Run ID: {dq['run_id'].iloc[0]} · WARN = issue handled and visible; "
                   "FAIL = output should not be trusted.")

    st.subheader("Coverage by city")
    cov = (df.groupby("city_id").agg(days=("date", "count"), weather_days=("has_weather", "sum"),
                                     aq_days=("has_air_quality", "sum"))
           .rename(index=city_name))
    cov["weather %"] = (100 * cov["weather_days"] / cov["days"]).round(1)
    cov["air quality %"] = (100 * cov["aq_days"] / cov["days"]).round(1)
    st.dataframe(cov, width="stretch")

    st.subheader("Known limitations")
    st.markdown(
        "- **Model data, not ground stations.** Weather is reanalysis and air quality is the CAMS model, "
        "both on grids roughly 10–25 km wide.\n"
        "- **Heavy rain is under-counted.** Grid averaging smooths local cloudbursts, so the IMD 64.5 mm "
        "threshold triggers less often than at a rain gauge. Use the rainfall slider to test lower values.\n"
        "- **One point per city.** Conditions can differ across a large city.\n"
        "- **Heat rule simplified.** IMD also considers departure from normal; a fixed 40 °C is used here."
    )

# ---------------------------------------------------------------------------
# Data explorer
# ---------------------------------------------------------------------------
with tab_data:
    st.subheader("Monthly summary")
    st.dataframe(monthly, width="stretch", hide_index=True)
    st.download_button("Download monthly summary (CSV)", monthly.to_csv(index=False),
                       file_name="city_month_summary.csv", mime="text/csv")
    st.subheader("Daily data")
    daily_cols = ["city_id", "date", "temp_max", "temp_min", "precipitation_mm", "wind_max_kmh",
                  "pm25_mean", "pm10_mean", "is_heat_day", "is_heavy_rain_day", "is_poor_air_day",
                  "is_disruption_day"]
    st.dataframe(df[daily_cols], width="stretch", hide_index=True)
    st.download_button("Download daily data (CSV)", df[daily_cols].to_csv(index=False),
                       file_name="city_daily.csv", mime="text/csv")
