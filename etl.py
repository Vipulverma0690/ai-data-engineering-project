"""ETL entry point.

Usage:
    python etl.py            # use cached raw data where available
    python etl.py --refresh  # force re-download from the API
"""
import argparse
import logging
import sys

from src.extract import run_extraction


def main() -> int:
    parser = argparse.ArgumentParser(description="City Operations Risk Monitor - ETL")
    parser.add_argument("--refresh", action="store_true", help="Re-download raw data from the API")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    log = logging.getLogger("etl")

    # ---- 1. Extract ----
    log.info("Step 1/1: Extracting from Open-Meteo...")
    manifest = run_extraction(refresh=args.refresh)

    failed = [m for m in manifest if m["status"] == "failed"]
    ok = len(manifest) - len(failed)
    log.info("Extraction finished: %d OK, %d failed (of %d).", ok, len(failed), len(manifest))

    if ok == 0:
        log.error("All extractions failed - check your internet connection. Stopping.")
        return 1
    if failed:
        log.warning("Continuing with partial data. Failed: %s",
                    ", ".join(f"{m['source']}/{m['city_id']}" for m in failed))

    # Transform, data-quality checks and load will be added in Step 4.
    return 0


if __name__ == "__main__":
    sys.exit(main())