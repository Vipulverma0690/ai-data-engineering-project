"""ETL entry point: Extract -> Transform -> Quality checks -> Load.

Usage:
    python etl.py            # use cached raw data where available
    python etl.py --refresh  # force re-download from the API
    python etl.py --strict   # exit with code 2 if any ERROR-level data-quality check fails
"""
import argparse
import logging
import sys

from src import config, quality, storage, transform
from src.extract import run_extraction


def main() -> int:
    parser = argparse.ArgumentParser(description="City Operations Risk Monitor - ETL")
    parser.add_argument("--refresh", action="store_true", help="Re-download raw data from the API")
    parser.add_argument("--strict", action="store_true", help="Fail if any ERROR-level DQ check fails")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("etl")

    # ---- 1. Extract ----
    log.info("Step 1/4: Extracting from Open-Meteo...")
    manifest = run_extraction(refresh=args.refresh)
    failed = [m for m in manifest if m["status"] == "failed"]
    log.info("Extraction finished: %d OK, %d failed (of %d).",
             len(manifest) - len(failed), len(failed), len(manifest))

    # ---- 2. Transform ----
    log.info("Step 2/4: Transforming raw files...")
    weather_raw, aq_raw, availability = transform.load_sources()
    if weather_raw.empty and aq_raw.empty:
        log.error("No usable raw data at all - check your internet connection and run again. Stopping.")
        return 1

    dim_city = transform.build_dim_city()
    dim_date = transform.build_dim_date()

    # Source DQ checks run BEFORE cleaning, so the report reflects what the API actually sent
    results = quality.check_availability(availability)
    results += quality.check_weather_source(weather_raw, dim_date["date"])
    results += quality.check_air_quality_source(aq_raw)

    weather = transform.clean_weather(weather_raw)
    aq_daily = transform.aggregate_air_quality(transform.clean_air_quality(aq_raw))
    fact = transform.build_fact_city_daily(dim_city, dim_date, weather, aq_daily)
    mart = transform.build_mart_city_month(fact, dim_city, dim_date)

    # ---- 3. Model DQ checks ----
    log.info("Step 3/4: Running data-quality checks...")
    results += quality.check_model(dim_city, dim_date, aq_daily, fact, mart)
    report = quality.build_report(results)

    # ---- 4. Load ----
    log.info("Step 4/4: Saving outputs...")
    tables = {
        "dim_city": dim_city,
        "dim_date": dim_date,
        "fact_weather_daily": transform.stamp(weather),
        "fact_air_quality_daily": transform.stamp(aq_daily),
        "fact_city_daily": fact,
        "mart_city_month": mart,
    }
    for name, df in tables.items():
        storage.save_table(df, name)
        log.info("  saved %-24s %6d rows", name, len(df))
    storage.save_dq_report(report)

    # ---- Summary ----
    counts = report["status"].value_counts().to_dict()
    log.info("Data quality: %d PASS, %d WARN, %d FAIL  (report: %s)",
             counts.get("PASS", 0), counts.get("WARN", 0), counts.get("FAIL", 0),
             storage.DQ_REPORT_PATH.relative_to(config.ROOT_DIR))
    for _, r in report[report["status"] != "PASS"].iterrows():
        log.warning("  [%s] %s %s: %d rows. %s", r["status"], r["check_id"], r["check_name"],
                    r["rows_affected"], r["details"])

    if args.strict and counts.get("FAIL", 0):
        log.error("Strict mode: ERROR-level data-quality checks failed.")
        return 2
    log.info("ETL complete. Next: streamlit run app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
