import os
import time
import logging
import threading
from typing import List, Literal, Dict, Any, Optional
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException, Query

# Logging konfigurieren
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT
)
logger = logging.getLogger(__name__)

# Uvicorn-Logger umkonfigurieren (für Production-Mode)
for logger_name in ['uvicorn', 'uvicorn.access', 'uvicorn.error']:
    uvicorn_logger = logging.getLogger(logger_name)
    uvicorn_logger.handlers.clear()
    handler = logging.StreamHandler()
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    handler.setFormatter(formatter)
    uvicorn_logger.addHandler(handler)
    uvicorn_logger.setLevel(logging.INFO)
    uvicorn_logger.propagate = False

APP_TITLE = "Energyforecast.de Proxy (48h/96h)"
API_BASE = "https://www.energyforecast.de/api/v1/predictions"

app = FastAPI(title=APP_TITLE)


# ---------- Hilfsfunktionen ----------
def _endpoint_for_horizon(horizon_hours: int) -> str:
    """Wählt den richtigen Endpunkt je nach Stundenhorizont."""
    if horizon_hours == 48:
        return f"{API_BASE}/next_48_hours"
    return f"{API_BASE}/next_96_hours"


def _to_utc_z(dt_str: str) -> str:
    """Wandelt ISO-String mit Zeitzone in UTC-Z-Format."""
    dt = datetime.fromisoformat(dt_str)
    dt_utc = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt_utc.isoformat().replace("+00:00", "Z")


def _res_to_api(resolution: str) -> str:
    """Mappt Benutzer-Parameter auf API-Feld."""
    r = resolution.strip().lower()
    if r in ("hourly", "hour", "stunde"):
        return "HOURLY"
    if r in ("quarter_hourly", "quarter-hourly", "viertelstunde", "15min", "15-min"):
        return "QUARTER_HOURLY"
    return "HOURLY"


def _vat_to_percent(vat: float) -> float:
    """Konvertiert z. B. 0.19 → 19.0 falls nötig."""
    return vat * 100.0 if vat <= 1.0 else vat


def _fixed_to_cent(fixed_cost_eur: float) -> float:
    """Konvertiert Fixkosten Euro → Cent (API erwartet Cent)."""
    return round(fixed_cost_eur * 100.0, 6)


def _ttl_bucket(ttl_minutes: int) -> int:
    """Erzeugt groben Zeit-Bucket für Cache-Invalidierung."""
    seconds = max(1, int(ttl_minutes) * 60)
    return int(time.time() // seconds)


# ---------- Cache-Infrastruktur ----------
# Struktur eines Eintrags: {"data": [...], "bucket": int, "has_next_day": bool}
_price_cache: Dict[tuple, dict] = {}
_cache_lock = threading.Lock()
_cache_stats = {"hits": 0, "misses": 0}


def _in_no_cache_window() -> bool:
    """True zwischen 12:00:00 UTC und 13:29:59 UTC (kein Caching)."""
    now = datetime.now(timezone.utc)
    return (now.hour == 12) or (now.hour == 13 and now.minute < 30)


# ---------- API Fetch (ohne Cache) ----------
def _fetch_prices_from_api(
    api_url: str,
    resolution_api: str,
    token: str,
    fixed_cost_cent: float,
    vat_percent: float,
    convert_to_utc: bool = True,
) -> List[dict]:
    """Holt Daten direkt von der Energyforecast-API (kein Cache)."""
    params = {
        "token": token,
        "resolution": resolution_api,
        "fixed_cost_cent": fixed_cost_cent,
        "vat": vat_percent,
    }
    logger.info(f"Fetching prices from {api_url} with resolution={resolution_api}")
    try:
        resp = requests.get(api_url, params=params, timeout=15)
    except requests.RequestException as e:
        logger.error(f"Upstream request failed: {e}")
        raise RuntimeError(f"Upstream request failed: {e}") from e

    if resp.status_code != 200:
        logger.error(f"Upstream returned {resp.status_code}: {resp.text[:300]}")
        raise RuntimeError(f"Upstream returned {resp.status_code}: {resp.text[:300]}")

    try:
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("Unexpected JSON shape (expected a list).")
    except ValueError as e:
        logger.error(f"Invalid JSON from upstream: {e}")
        raise RuntimeError(f"Invalid JSON from upstream: {e}") from e

    out = []
    for item in data:
        if convert_to_utc:
            start = _to_utc_z(item["start"])
            end = _to_utc_z(item["end"])
        else:
            start = item["start"]
            end = item["end"]
        value = float(item["price"])  # Euro/kWh
        out.append({"start": start, "end": end, "value": value})
    logger.info(f"Successfully fetched {len(out)} price entries")
    return out


# ---------- Cache-bewusster Abruf ----------
def _get_prices_cached(
    api_url: str,
    resolution_api: str,
    token: str,
    fixed_cost_cent: float,
    vat_percent: float,
    convert_to_utc: bool,
    ttl_minutes: int,
) -> List[dict]:
    """
    Gibt gecachte Preise zurück oder holt neue Daten von der API.

    Kein-Cache-Fenster (12:00–13:30 UTC):
      - Cache wird weder gelesen noch geschrieben.
      - Jeder Request holt frische Daten von der API.

    Außerhalb des Fensters: normales TTL-basiertes Caching.
    """
    key = (api_url, resolution_api, token, fixed_cost_cent, vat_percent, convert_to_utc)

    if not _in_no_cache_window():
        now_bucket = _ttl_bucket(ttl_minutes)
        with _cache_lock:
            entry = _price_cache.get(key)
        if entry is not None and entry["bucket"] == now_bucket:
            _cache_stats["hits"] += 1
            return entry["data"]
    else:
        logger.info("No-cache window aktiv (12:00–13:30 UTC) – Cache wird umgangen")

    _cache_stats["misses"] += 1
    data = _fetch_prices_from_api(api_url, resolution_api, token, fixed_cost_cent, vat_percent, convert_to_utc)

    if not _in_no_cache_window():
        with _cache_lock:
            _price_cache[key] = {"data": data, "bucket": _ttl_bucket(ttl_minutes)}

    return data


# ---------- Hauptendpunkte ----------
@app.get(
    "/prices",
    response_model=Dict[str, List[dict]],  # { "prices": [ ... ] }
    responses={
        200: {"description": "OK"},
        500: {"description": "Fehler beim Abruf oder bei der Verarbeitung"},
    },
)
def get_prices(
    horizon: int = Query(
        96, description="Zeithorizont in Stunden (48 oder 96)."
    ),
    resolution: Literal["hourly", "quarter_hourly"] = Query(
        "hourly", description="Zeitauflösung"
    ),
    token: str = Query(..., description="API-Token für energyforecast.de"),
    fixed_cost: float = Query(
        0.0,
        description="Fixkosten in Euro/kWh (z. B. 0.16774 für 16.774 ct/kWh)"
    ),
    vat: float = Query(
        0.19, description="Mehrwertsteuer (0.19 oder 19)."
    ),
    cache_ttl_minutes: int = Query(
        60, ge=1, le=24 * 60, description="Cache-Gültigkeit in Minuten"
    ),
    resultformat: Literal["default", "evcc"] = Query(
        "default", description="Ausgabeformat: 'default' für {prices: [...]}, 'evcc' für {rates: [...]}"
    ),
    price_cap: Optional[float] = Query(
        None, description="Preisobergrenze in EUR/kWh nach Steuern und Gebühren. Preise darüber werden auf diesen Wert gedeckelt."
    ),
):
    """
    Proxy für:
      - /api/v1/predictions/next_48_hours
      - /api/v1/predictions/next_96_hours

    Antwort (default Format):
    {
      "prices": [
        { "start": "...Z", "end": "...Z", "value": <EUR/kWh> },
        ...
      ]
    }

    Antwort (evcc Format):
    {
      "rates": [
        { "start": "...", "end": "...", "value": <EUR/kWh> },
        ...
      ]
    }
    """
    try:
        if horizon not in (48, 96):
            logger.warning(f"Invalid horizon requested: {horizon}")
            raise HTTPException(status_code=400, detail="horizon must be 48 or 96")

        logger.info(f"Request: horizon={horizon}, resolution={resolution}, fixed_cost={fixed_cost}, vat={vat}")
        api_url = _endpoint_for_horizon(horizon)
        resolution_api = _res_to_api(resolution)
        vat_percent = _vat_to_percent(vat)
        fixed_cost_cent = _fixed_to_cent(fixed_cost)

        # evcc Format benötigt Original-Zeitstempel, default Format UTC-Z
        convert_to_utc = (resultformat == "default")

        prices = _get_prices_cached(
            api_url,
            resolution_api,
            token,
            fixed_cost_cent,
            vat_percent,
            convert_to_utc,
            cache_ttl_minutes,
        )

        if price_cap is not None:
            prices = [
                {**p, "value": min(p["value"], price_cap)}
                for p in prices
            ]
            logger.info(f"Applied price_cap={price_cap} EUR/kWh")

        logger.info(f"Returning {len(prices)} prices for horizon={horizon}, format={resultformat}")

        # Format-Konvertierung
        if resultformat == "evcc":
            return {"rates": prices}

        return {"prices": prices}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in get_prices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health", tags=["system"])
def health() -> Dict[str, Any]:
    """
    Einfache Healthcheck-Route.
    Gibt 200 OK und Cache-Statusinformationen zurück.
    """
    logger.debug("Health check requested")
    with _cache_lock:
        cached_entries = len(_price_cache)
    return {
        "status": "ok",
        "service": APP_TITLE,
        "cached_entries": cached_entries,
        "cache_hits": _cache_stats["hits"],
        "cache_misses": _cache_stats["misses"],
        "no_cache_window_active": _in_no_cache_window(),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


# ---------- Lokaler Start ----------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    logger.info(f"Starting server on port {port}")

    # Uvicorn Log-Konfiguration - gleiches Format wie beim App-Logger
    log_config = uvicorn.config.LOGGING_CONFIG
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    log_config["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
    log_config["formatters"]["access"]["fmt"] = '%(asctime)s - %(name)s - %(levelname)s - %(client_addr)s - "%(request_line)s" %(status_code)s'
    log_config["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_config=log_config,
    )
