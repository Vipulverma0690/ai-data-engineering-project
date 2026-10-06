"""Read/write helpers for processed tables. Used by both etl.py and app.py."""
from __future__ import annotations

import pandas as pd

from src import config

TABLES = ["dim_city", "dim_date", "fact_weather_daily", "fact_air_quality_daily",
          "fact_city_daily", "mart_city_month"]
DQ_REPORT_PATH = config.QUALITY_DIR / "dq_report.csv"


def save_table(df: pd.DataFrame, name: str) -> None:
    """Parquet keeps data types (dates, booleans, nulls) intact, unlike CSV."""
    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(config.PROCESSED_DIR / f"{name}.parquet", index=False)


def load_table(name: str) -> pd.DataFrame:
    return pd.read_parquet(config.PROCESSED_DIR / f"{name}.parquet")


def save_dq_report(report: pd.DataFrame) -> None:
    """CSV so it can be opened in Excel or viewed directly on GitHub."""
    config.QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    report.to_csv(DQ_REPORT_PATH, index=False)


def load_dq_report() -> pd.DataFrame:
    return pd.read_csv(DQ_REPORT_PATH)
