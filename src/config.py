"""Central configuration: cities, date range, API settings and business thresholds."""
from pathlib import Path

# ---------- Paths ----------
ROOT_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT_DIR / "data" / "raw"
PROCESSED_DIR = ROOT_DIR / "data" / "processed"
QUALITY_DIR = ROOT_DIR / "data" / "quality"

# ---------- Scope ----------
START_DATE = "2024-10-01"
END_DATE = "2026-09-30"
TIMEZONE = "Asia/Kolkata"

CITIES = [
    {"city_id": "DEL", "city": "Delhi",     "state": "Delhi",       "region": "North", "latitude": 28.61, "longitude": 77.21},
    {"city_id": "JAI", "city": "Jaipur",    "state": "Rajasthan",   "region": "North", "latitude": 26.91, "longitude": 75.79},
    {"city_id": "MUM", "city": "Mumbai",    "state": "Maharashtra", "region": "West",  "latitude": 19.08, "longitude": 72.88},
    {"city_id": "AMD", "city": "Ahmedabad", "state": "Gujarat",     "region": "West",  "latitude": 23.02, "longitude": 72.57},
    {"city_id": "KOL", "city": "Kolkata",   "state": "West Bengal", "region": "East",  "latitude": 22.57, "longitude": 88.36},
    {"city_id": "CHE", "city": "Chennai",   "state": "Tamil Nadu",  "region": "South", "latitude": 13.08, "longitude": 80.27},
    {"city_id": "BLR", "city": "Bengaluru", "state": "Karnataka",   "region": "South", "latitude": 12.97, "longitude": 77.59},
    {"city_id": "HYD", "city": "Hyderabad", "state": "Telangana",   "region": "South", "latitude": 17.39, "longitude": 78.49},
]

# ---------- API ----------
WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

WEATHER_DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "temperature_2m_mean",
    "precipitation_sum",
    "wind_speed_10m_max",
]
AIR_QUALITY_HOURLY_VARS = ["pm2_5", "pm10"]

REQUEST_TIMEOUT_SEC = 60
MAX_RETRIES = 3
BACKOFF_SEC = 2  # wait 2s, 4s, 8s between retries

# ---------- Business thresholds ----------
HEAT_DAY_TEMP_MAX_C = 40.0   # IMD heatwave (simplified)
HEAVY_RAIN_MM = 64.5         # IMD "heavy rainfall" category
PM25_LIMIT_UGM3 = 60.0       # NAAQS 24-hour standard
PM10_LIMIT_UGM3 = 100.0      # NAAQS 24-hour standard

# ---------- Data quality ranges (physically plausible for India) ----------
VALID_RANGES = {
    "temp_max": (-10, 55),
    "temp_min": (-20, 45),
    "temp_mean": (-15, 50),
    "precipitation_mm": (0, 500),
    "wind_max_kmh": (0, 250),
    "pm25_mean": (0, 1000),
    "pm10_mean": (0, 2000),
}
MIN_AQ_HOURS_PER_DAY = 18  # a daily air-quality average needs at least 18 of 24 hours
