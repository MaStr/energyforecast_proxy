import os
import time
import logging
import threading
from typing import List, Literal, Dict, Any, Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

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
DYN_NET_API_URL_DEFAULT = "https://dyn-net.batcontrol.software/api"

app = FastAPI(title=APP_TITLE)


# ---------- Simple-Modus: Konfiguration aus Umgebungsvariablen ----------
def _load_simple_config() -> Optional[dict]:
    """
    Liest Konfiguration für den Simple-Modus aus Umgebungsvariablen.
    Gibt None zurück, wenn ENERGYFORECAST_TOKEN nicht gesetzt ist.

    Variablen:
      ENERGYFORECAST_TOKEN              API-Token (Pflicht für Simple-Modus)
      ENERGYFORECAST_HORIZON            48 oder 96 (Standard: 96)
      ENERGYFORECAST_RESOLUTION         hourly | quarter_hourly (Standard: hourly)
      ENERGYFORECAST_FIXED_NET_COST     Netzgebühren in EUR/kWh, z. B. 0.08 (Standard: 0.0)
      ENERGYFORECAST_MARKUP_COSTS       Anbieter-Aufschlag in EUR/kWh, z. B. 0.01 (Standard: 0.0)
      ENERGYFORECAST_FIXED_COST_OTHER   Sonstige Fixkosten in EUR/kWh (Standard: 0.0)
      ENERGYFORECAST_VAT                z. B. 0.19 oder 19 (Standard: 0.19)
      ENERGYFORECAST_PRICE_CAP          EUR/kWh, optional
      ENERGYFORECAST_CACHE_TTL          Minuten (Standard: 60)
      ENERGYFORECAST_RESULT_FORMAT      default | evcc (Standard: default)
      ENERGYFORECAST_TZ                 IANA-Zeitzone, z. B. Europe/Berlin (optional)
      ENERGYFORECAST_DYN_NET_OPERATOR   §14a Netzbetreiber-ID, z. B. syna (optional, überschreibt fixed_net_cost)
      ENERGYFORECAST_DYN_NET_COUNTRY    Ländercode für Netzgebühren-API (Standard: de)
      ENERGYFORECAST_DYN_NET_API_URL    Basis-URL der dynamic_energy_fees API (Standard: DYN_NET_API_URL_DEFAULT)
    """
    token = os.getenv("ENERGYFORECAST_TOKEN")
    if not token:
        return None

    price_cap_raw = os.getenv("ENERGYFORECAST_PRICE_CAP")
    tz_raw = os.getenv("ENERGYFORECAST_TZ")

    return {
        "token": token,
        "horizon": int(os.getenv("ENERGYFORECAST_HORIZON", "96")),
        "resolution": os.getenv("ENERGYFORECAST_RESOLUTION", "hourly"),
        "fixed_net_cost": float(os.getenv("ENERGYFORECAST_FIXED_NET_COST", "0.0")),
        "markup_costs": float(os.getenv("ENERGYFORECAST_MARKUP_COSTS", "0.0")),
        "fixed_cost_other": float(os.getenv("ENERGYFORECAST_FIXED_COST_OTHER", "0.0")),
        "vat": float(os.getenv("ENERGYFORECAST_VAT", "0.19")),
        "price_cap": float(price_cap_raw) if price_cap_raw else None,
        "cache_ttl_minutes": int(os.getenv("ENERGYFORECAST_CACHE_TTL", "60")),
        "resultformat": os.getenv("ENERGYFORECAST_RESULT_FORMAT", "default"),
        "tz": tz_raw if tz_raw else None,
        "dyn_net_operator": os.getenv("ENERGYFORECAST_DYN_NET_OPERATOR"),
        "dyn_net_country": os.getenv("ENERGYFORECAST_DYN_NET_COUNTRY", "de"),
        "dyn_net_api_url": os.getenv("ENERGYFORECAST_DYN_NET_API_URL", DYN_NET_API_URL_DEFAULT),
    }


_SIMPLE_CONFIG: Optional[dict] = None


@app.on_event("startup")
def _init_simple_config():
    global _SIMPLE_CONFIG
    _SIMPLE_CONFIG = _load_simple_config()
    if _SIMPLE_CONFIG:
        logger.info(
            f"Simple-Modus aktiv: horizon={_SIMPLE_CONFIG['horizon']}, "
            f"resolution={_SIMPLE_CONFIG['resolution']}, "
            f"fixed_net_cost={_SIMPLE_CONFIG['fixed_net_cost']}, "
            f"markup_costs={_SIMPLE_CONFIG['markup_costs']}, "
            f"fixed_cost_other={_SIMPLE_CONFIG['fixed_cost_other']}, "
            f"vat={_SIMPLE_CONFIG['vat']}, "
            f"format={_SIMPLE_CONFIG['resultformat']}, "
            f"tz={_SIMPLE_CONFIG['tz']}"
        )
        if _SIMPLE_CONFIG.get("dyn_net_operator"):
            logger.info(
                f"Dynamische Netzgebühren aktiv: operator={_SIMPLE_CONFIG['dyn_net_operator']}, "
                f"country={_SIMPLE_CONFIG['dyn_net_country']}, "
                f"api_url={_SIMPLE_CONFIG['dyn_net_api_url']}"
            )
        else:
            logger.info("Dynamische Netzgebühren inaktiv (ENERGYFORECAST_DYN_NET_OPERATOR nicht gesetzt)")
    else:
        logger.info("Simple-Modus inaktiv (ENERGYFORECAST_TOKEN nicht gesetzt)")


# ---------- Hilfsfunktionen ----------
def _endpoint_for_horizon(horizon_hours: int) -> str:
    """Wählt den richtigen Endpunkt je nach Stundenhorizont."""
    if horizon_hours == 48:
        return f"{API_BASE}/next_48_hours"
    return f"{API_BASE}/next_96_hours"


def _convert_timestamp(dt_str: str, tz_name: Optional[str]) -> str:
    """
    Konvertiert einen ISO-Zeitstempel in die gewünschte Zeitzone.
    tz_name=None  → Zeitstempel unverändert zurückgeben
    tz_name="UTC" → UTC mit Z-Suffix (z. B. 2024-01-01T13:00:00Z)
    tz_name=...   → Lokale Zeit mit Offset (z. B. 2024-01-01T14:00:00+01:00)
    """
    if tz_name is None:
        return dt_str
    dt = datetime.fromisoformat(dt_str).astimezone(ZoneInfo(tz_name)).replace(microsecond=0)
    iso = dt.isoformat()
    return iso.replace("+00:00", "Z") if tz_name == "UTC" else iso


def _res_to_api(resolution: str) -> str:
    """Mappt Benutzer-Parameter auf API-Feld."""
    r = resolution.strip().lower()
    if r in ("hourly", "hour", "stunde"):
        return "HOURLY"
    if r in ("quarter_hourly", "quarter-hourly", "viertelstunde", "15min", "15-min"):
        return "QUARTER_HOURLY"
    return "HOURLY"


def _apply_costs(
    raw_prices: List[dict],
    fixed_net_cost: float,
    markup_costs: float,
    fixed_cost_other: float,
    vat: float,
) -> List[dict]:
    """
    Wendet Fixkosten und MwSt. lokal auf Netto-Spotpreise an.
    Formel: (spot + fixed_net_cost + markup_costs + fixed_cost_other) * (1 + vat)
    vat kann als Faktor (0.19) oder Prozent (19) übergeben werden.
    """
    vat_factor = (vat / 100.0) if vat > 1.0 else vat
    total_fixed = fixed_net_cost + markup_costs + fixed_cost_other
    return [
        {**p, "value": round((p["value"] + total_fixed) * (1 + vat_factor), 6)}
        for p in raw_prices
    ]


def _ttl_bucket(ttl_minutes: int) -> int:
    """Erzeugt groben Zeit-Bucket für Cache-Invalidierung."""
    seconds = max(1, int(ttl_minutes) * 60)
    return int(time.time() // seconds)


# ---------- Cache-Infrastruktur ----------
# Struktur eines Eintrags: {"data": [...], "bucket": int, "has_next_day": bool}
_price_cache: Dict[tuple, dict] = {}
_cache_lock = threading.Lock()
_cache_stats = {"hits": 0, "misses": 0}
_request_counts: Dict[str, int] = {"prices": 0, "current": 0}

_fee_cache: Dict[tuple, dict] = {}
_fee_cache_lock = threading.Lock()
_fee_cache_stats = {"hits": 0, "misses": 0}


def _in_no_cache_window() -> bool:
    """True zwischen 12:00:00 UTC und 13:29:59 UTC (kein Caching)."""
    now = datetime.now(timezone.utc)
    return (now.hour == 12) or (now.hour == 13 and now.minute < 30)


# ---------- API Fetch (ohne Cache) ----------
def _fetch_prices_from_api(
    api_url: str,
    resolution_api: str,
    token: str,
    output_tz: Optional[str] = "UTC",
) -> List[dict]:
    """Holt Netto-Spotpreise direkt von der Energyforecast-API (kein Cache, keine Kostenberechnung).
    fixed_cost_cent=0 und vat=0 werden explizit übergeben, damit die API den reinen Spotpreis
    zurückgibt – unabhängig davon ob die Parameter serverseitig Pflicht sind."""
    params = {
        "token": token,
        "resolution": resolution_api,
        "fixed_cost_cent": 0,
        "vat": 0,
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
        start = _convert_timestamp(item["start"], output_tz)
        end = _convert_timestamp(item["end"], output_tz)
        value = float(item["price"])  # Euro/kWh
        out.append({"start": start, "end": end, "value": value})
    logger.info(f"Successfully fetched {len(out)} price entries")
    return out


# ---------- Dynamische Netzgebühren ----------
def _fetch_net_fees(api_url: str, operator: str, country: str, next_hours: int) -> List[dict]:
    """Holt zeitvariable Netzgebühren-Slots von der dynamic_energy_fees-API.
    Timestamps werden sofort zu UTC normalisiert (als datetime-Objekte)."""
    params = {"country": country, "operator": operator, "next_hours": next_hours}
    logger.info(f"Fetching dynamic net fees from {api_url}: operator={operator}, country={country}")
    try:
        resp = requests.get(api_url, params=params, timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Dynamic fee API request failed: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"Dynamic fee API returned {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("Expected a JSON array from fee API.")
    except ValueError as e:
        raise RuntimeError(f"Invalid JSON from fee API: {e}") from e
    slots = []
    for item in data:
        start_dt = datetime.fromisoformat(item["start"]).astimezone(timezone.utc)
        end_dt = datetime.fromisoformat(item["end"]).astimezone(timezone.utc)
        slots.append({"start": start_dt, "end": end_dt, "value": float(item["value"])})
    logger.info(f"Fetched {len(slots)} dynamic fee slots")
    return slots


def _get_net_fees_cached(
    api_url: str, operator: str, country: str, next_hours: int, ttl_minutes: int
) -> List[dict]:
    """Gibt gecachte Fee-Slots zurück; bei Fehler leere Liste (Fallback auf fixed_net_cost)."""
    key = (api_url, operator, country, next_hours)
    now_bucket = _ttl_bucket(ttl_minutes)
    with _fee_cache_lock:
        entry = _fee_cache.get(key)
    if entry is not None and entry["bucket"] == now_bucket:
        _fee_cache_stats["hits"] += 1
        return entry["data"]
    _fee_cache_stats["misses"] += 1
    try:
        data = _fetch_net_fees(api_url, operator, country, next_hours)
    except RuntimeError as e:
        logger.warning(f"Dynamic fee fetch failed, falling back to fixed_net_cost: {e}")
        return []
    with _fee_cache_lock:
        _fee_cache[key] = {"data": data, "bucket": now_bucket}
    return data


def _resolve_net_cost_for_slot(price_start_str: str, fee_slots: List[dict], fallback: float = 0.0) -> float:
    """Sucht den passenden Fee-Slot für einen Preis-Slot-Startzeitpunkt."""
    s = price_start_str
    price_start = datetime.fromisoformat(
        s[:-1] + "+00:00" if s.endswith("Z") else s
    ).astimezone(timezone.utc)
    for slot in fee_slots:
        if slot["start"] <= price_start < slot["end"]:
            return slot["value"]
    logger.debug(f"No fee slot found for {price_start_str}, using fallback {fallback}")
    return fallback


def _apply_costs_dynamic(
    raw_prices: List[dict],
    fee_slots: List[dict],
    markup_costs: float,
    fixed_cost_other: float,
    vat: float,
    fallback_net_cost: float = 0.0,
) -> List[dict]:
    """Wie _apply_costs(), aber mit zeitslot-genauer Netzgebühr aus fee_slots.
    Formel: (spot + net_fee + markup_costs + fixed_cost_other) * (1 + vat)"""
    vat_factor = (vat / 100.0) if vat > 1.0 else vat
    result = []
    for p in raw_prices:
        net_fee = _resolve_net_cost_for_slot(p["start"], fee_slots, fallback=fallback_net_cost)
        total_fixed = net_fee + markup_costs + fixed_cost_other
        result.append({**p, "value": round((p["value"] + total_fixed) * (1 + vat_factor), 6)})
    return result


def _select_costs(
    raw_prices: List[dict],
    fixed_net_cost: float,
    markup_costs: float,
    fixed_cost_other: float,
    vat: float,
    dyn_net_operator: Optional[str],
    dyn_net_country: str,
    dyn_net_api_url: str,
    horizon: int,
    cache_ttl_minutes: int,
) -> List[dict]:
    """Wendet statische oder dynamische Netzgebühren an – je nachdem ob dyn_net_operator gesetzt ist."""
    if dyn_net_operator:
        fee_slots = _get_net_fees_cached(
            dyn_net_api_url, dyn_net_operator, dyn_net_country, horizon, cache_ttl_minutes
        )
        return _apply_costs_dynamic(
            raw_prices, fee_slots, markup_costs, fixed_cost_other, vat,
            fallback_net_cost=fixed_net_cost,
        )
    return _apply_costs(raw_prices, fixed_net_cost, markup_costs, fixed_cost_other, vat)


# ---------- Cache-bewusster Abruf ----------
def _get_prices_cached(
    api_url: str,
    resolution_api: str,
    token: str,
    output_tz: Optional[str],
    ttl_minutes: int,
    skip_cache_read: bool = False,
) -> List[dict]:
    """
    Gibt gecachte Netto-Spotpreise zurück oder holt neue Daten von der API.
    Der Cache speichert ausschließlich Rohdaten – Fixkosten und MwSt. werden
    nach dem Cache-Zugriff lokal durch _apply_costs() berechnet.

    skip_cache_read=True  → Cache-Lesen überspringen, frisch von der API holen,
                            Ergebnis trotzdem in den Cache schreiben.
    skip_cache_read=False → Normales TTL-Caching.
    """
    key = (api_url, resolution_api, token, output_tz)
    now_bucket = _ttl_bucket(ttl_minutes)

    if not skip_cache_read:
        with _cache_lock:
            entry = _price_cache.get(key)
        if entry is not None and entry["bucket"] == now_bucket:
            _cache_stats["hits"] += 1
            return entry["data"]
    else:
        logger.info("No-cache window aktiv (12:00–13:30 UTC) – Cache-Lesen übersprungen")

    _cache_stats["misses"] += 1
    data = _fetch_prices_from_api(api_url, resolution_api, token, output_tz)

    with _cache_lock:
        _price_cache[key] = {"data": data, "bucket": now_bucket}

    return data


# ---------- Hauptendpunkte ----------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """Nutzungshinweis für den Proxy."""
    return """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Energyforecast Proxy</title>
  <style>
    body { font-family: sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; color: #222; }
    h1 { font-size: 1.5rem; }
    h2 { font-size: 1.1rem; margin-top: 2em; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
    table { border-collapse: collapse; width: 100%; margin-top: 0.5em; }
    th, td { text-align: left; padding: 6px 10px; border: 1px solid #ddd; font-size: 0.9rem; }
    th { background: #f5f5f5; }
    code { background: #f0f0f0; padding: 2px 5px; border-radius: 3px; font-size: 0.9em; }
    pre { background: #f0f0f0; padding: 12px; border-radius: 4px; overflow-x: auto; font-size: 0.85em; }
    .note { background: #fff8e1; border-left: 3px solid #f0ad00; padding: 8px 12px; margin-top: 1em; font-size: 0.9em; }
  </style>
</head>
<body>
  <h1>Energyforecast.de Proxy</h1>
  <p>Proxy für die <a href="https://www.energyforecast.de">energyforecast.de</a> API.
     Liefert Strompreis-Vorhersagen mit optionalem Fixkosten-Aufschlag, MwSt. und Preisobergrenze.
     &nbsp;·&nbsp; <a href="https://github.com/MaStr/energyforecast_proxy">GitHub</a></p>

  <h2>Endpunkte</h2>
  <table>
    <tr><th>Pfad</th><th>Beschreibung</th></tr>
    <tr><td><code>GET /prices</code></td><td>Strompreis-Vorhersage abrufen (alle Parameter als Query-Parameter)</td></tr>
    <tr><td><code>GET /current</code></td><td>Aktuellen Preis abrufen (alle Parameter als Query-Parameter)</td></tr>
    <tr><td><code>GET /simple/prices</code></td><td>Alle Preise – Konfiguration aus Umgebungsvariablen</td></tr>
    <tr><td><code>GET /simple/current</code></td><td>Aktueller Preis – Konfiguration aus Umgebungsvariablen</td></tr>
    <tr><td><code>GET /health</code></td><td>Dienststatus und Cache-Statistiken</td></tr>
  </table>

  <h2>Simple-Modus (lokales Deployment)</h2>
  <p>Wenn <code>ENERGYFORECAST_TOKEN</code> gesetzt ist, sind <code>/simple/prices</code> und
     <code>/simple/current</code> ohne Query-Parameter nutzbar:</p>
  <table>
    <tr><th>Umgebungsvariable</th><th>Standard</th><th>Beschreibung</th></tr>
    <tr><td><code>ENERGYFORECAST_TOKEN</code></td><td><em>Pflicht</em></td><td>API-Token für energyforecast.de</td></tr>
    <tr><td><code>ENERGYFORECAST_HORIZON</code></td><td>96</td><td>48 oder 96 Stunden</td></tr>
    <tr><td><code>ENERGYFORECAST_RESOLUTION</code></td><td>hourly</td><td><code>hourly</code> oder <code>quarter_hourly</code></td></tr>
    <tr><td><code>ENERGYFORECAST_FIXED_NET_COST</code></td><td>0.0</td><td>Netzgebühren in EUR/kWh</td></tr>
    <tr><td><code>ENERGYFORECAST_MARKUP_COSTS</code></td><td>0.0</td><td>Anbieter-Aufschlag in EUR/kWh</td></tr>
    <tr><td><code>ENERGYFORECAST_FIXED_COST_OTHER</code></td><td>0.0</td><td>Sonstige Fixkosten in EUR/kWh</td></tr>
    <tr><td><code>ENERGYFORECAST_VAT</code></td><td>0.19</td><td>Mehrwertsteuer (0.19 oder 19)</td></tr>
    <tr><td><code>ENERGYFORECAST_PRICE_CAP</code></td><td>–</td><td>Preisobergrenze in EUR/kWh (optional)</td></tr>
    <tr><td><code>ENERGYFORECAST_CACHE_TTL</code></td><td>60</td><td>Cache-Gültigkeit in Minuten</td></tr>
    <tr><td><code>ENERGYFORECAST_RESULT_FORMAT</code></td><td>default</td><td><code>default</code> oder <code>evcc</code></td></tr>
    <tr><td><code>ENERGYFORECAST_TZ</code></td><td>–</td><td>Ausgabe-Zeitzone, z.&nbsp;B. <code>Europe/Berlin</code></td></tr>
    <tr><td><code>ENERGYFORECAST_DYN_NET_OPERATOR</code></td><td>–</td><td>§14a EnWG Netzbetreiber-ID (z.&nbsp;B. <code>syna</code>). Aktiviert zeitvariable Netzgebühren, überschreibt <code>ENERGYFORECAST_FIXED_NET_COST</code>.</td></tr>
    <tr><td><code>ENERGYFORECAST_DYN_NET_COUNTRY</code></td><td>de</td><td>Ländercode für die Netzgebühren-API</td></tr>
    <tr><td><code>ENERGYFORECAST_DYN_NET_API_URL</code></td><td><code>https://dyn-net.batcontrol.software/api</code></td><td>Nur bei Self-Hosting der dynamic_energy_fees-Instanz nötig</td></tr>
  </table>

  <h2>Parameter <code>/prices</code></h2>
  <table>
    <tr><th>Parameter</th><th>Typ</th><th>Standard</th><th>Beschreibung</th></tr>
    <tr><td><code>token</code></td><td>string</td><td><em>Pflicht</em></td><td>API-Token für energyforecast.de</td></tr>
    <tr><td><code>horizon</code></td><td>int</td><td>96</td><td>Zeithorizont in Stunden: <code>48</code> oder <code>96</code></td></tr>
    <tr><td><code>resolution</code></td><td>string</td><td>hourly</td><td><code>hourly</code> (stündlich) oder <code>quarter_hourly</code> (15 min)</td></tr>
    <tr><td><code>fixed_net_cost</code></td><td>float</td><td>0.0</td><td>Netzgebühren in EUR/kWh (z.&nbsp;B. <code>0.08</code>)</td></tr>
    <tr><td><code>markup_costs</code></td><td>float</td><td>0.0</td><td>Anbieter-Aufschlag in EUR/kWh (z.&nbsp;B. <code>0.01</code> für 1&nbsp;ct/kWh)</td></tr>
    <tr><td><code>fixed_cost_other</code></td><td>float</td><td>0.0</td><td>Sonstige Fixkosten in EUR/kWh</td></tr>
    <tr><td><code>vat</code></td><td>float</td><td>0.19</td><td>Mehrwertsteuer als Faktor (<code>0.19</code>) oder Prozent (<code>19</code>). Wird auf den Gesamtpreis (Spot + Fixkosten) angewendet.</td></tr>
    <tr><td><code>price_cap</code></td><td>float</td><td>–</td><td>Preisobergrenze in EUR/kWh nach Steuern und Gebühren. Preise darüber werden auf diesen Wert gedeckelt.</td></tr>
    <tr><td><code>cache_ttl_minutes</code></td><td>int</td><td>60</td><td>Cache-Gültigkeit in Minuten (1–1440). Zwischen 12:00 und 13:30 UTC wird der Cache immer umgangen.</td></tr>
    <tr><td><code>resultformat</code></td><td>string</td><td>default</td><td>Ausgabeformat – siehe unten</td></tr>
    <tr><td><code>tz</code></td><td>string</td><td>–</td><td>Ausgabe-Zeitzone für Zeitstempel (IANA-Name, z.&nbsp;B. <code>Europe/Berlin</code> oder <code>UTC</code>). Standard: <code>UTC</code> bei <code>default</code>-Format, Original-Offset der API bei <code>evcc</code>.</td></tr>
    <tr><td><code>dyn_net_operator</code></td><td>string</td><td>–</td><td>§14a EnWG Netzbetreiber-ID (z.&nbsp;B. <code>syna</code>). Aktiviert zeitvariable Netzgebühren, überschreibt <code>fixed_net_cost</code>.</td></tr>
    <tr><td><code>dyn_net_country</code></td><td>string</td><td>de</td><td>Ländercode für die Netzgebühren-API</td></tr>
    <tr><td><code>dyn_net_api_url</code></td><td>string</td><td><em>Default-URL</em></td><td>Basis-URL der dynamic_energy_fees API (überschreibbar für Self-Hosting)</td></tr>
  </table>

  <h2>Ausgabeformat: <code>resultformat</code></h2>
  <p><strong><code>default</code></strong> – Zeitstempel werden nach UTC konvertiert, Schlüssel ist <code>prices</code>:</p>
  <pre>{ "prices": [ { "start": "2024-01-01T12:00:00Z", "end": "2024-01-01T13:00:00Z", "value": 0.2843 } ] }</pre>

  <p><strong><code>evcc</code></strong> – Zeitstempel bleiben im Original-Format der API (mit Zeitzonenoffset), Schlüssel ist <code>rates</code>.
     Dieses Format ist kompatibel mit dem <a href="https://docs.evcc.io">evcc</a>-Tarif-Interface:</p>
  <pre>{ "rates": [ { "start": "2024-01-01T13:00:00+01:00", "end": "2024-01-01T14:00:00+01:00", "value": 0.2843 } ] }</pre>

  <div class="note">
    In beiden Formaten ist <code>value</code> der Endpreis in <strong>EUR/kWh</strong>
    (Spot-Preis + <code>fixed_net_cost</code> + <code>markup_costs</code> + <code>fixed_cost_other</code>, inkl. MwSt., ggf. gedeckelt durch <code>price_cap</code>).
  </div>

  <h2>Beispielaufruf</h2>
  <pre>GET /prices?token=DEIN_TOKEN&amp;horizon=96&amp;fixed_net_cost=0.08&amp;markup_costs=0.01&amp;vat=0.19&amp;resultformat=evcc</pre>
</body>
</html>"""


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
    fixed_net_cost: float = Query(0.0, description="Netzgebühren in EUR/kWh (z. B. 0.08)"),
    markup_costs: float = Query(0.0, description="Anbieter-Aufschlag in EUR/kWh (z. B. 0.01 für 1 ct/kWh)"),
    fixed_cost_other: float = Query(0.0, description="Sonstige Fixkosten in EUR/kWh"),
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
    tz: Optional[str] = Query(
        None, description="Ausgabe-Zeitzone für Zeitstempel (z. B. 'Europe/Berlin', 'UTC'). Standard: UTC für default-Format, Original-Offset für evcc."
    ),
    dyn_net_operator: Optional[str] = Query(
        None, description="§14a EnWG Netzbetreiber-ID (z. B. 'syna'). Aktiviert zeitvariable Netzgebühren, überschreibt fixed_net_cost."
    ),
    dyn_net_country: str = Query("de", description="Ländercode für die dynamic_energy_fees API (Standard: de)"),
    dyn_net_api_url: str = Query(DYN_NET_API_URL_DEFAULT, description="Basis-URL der dynamic_energy_fees API"),
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

        _request_counts["prices"] += 1
        if tz is not None:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                raise HTTPException(status_code=400, detail=f"Unbekannte Zeitzone: '{tz}'")
        logger.info(f"Request: horizon={horizon}, resolution={resolution}, net={fixed_net_cost}, markup={markup_costs}, other={fixed_cost_other}, vat={vat}")
        api_url = _endpoint_for_horizon(horizon)
        resolution_api = _res_to_api(resolution)
        output_tz = tz if tz is not None else ("UTC" if resultformat == "default" else None)

        raw_prices = _get_prices_cached(
            api_url, resolution_api, token, output_tz, cache_ttl_minutes,
            skip_cache_read=_in_no_cache_window(),
        )
        prices = _select_costs(
            raw_prices, fixed_net_cost, markup_costs, fixed_cost_other, vat,
            dyn_net_operator, dyn_net_country, dyn_net_api_url, horizon, cache_ttl_minutes,
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


@app.get(
    "/current",
    response_model=Dict[str, Any],
    responses={
        200: {"description": "OK"},
        404: {"description": "Kein Preis für den aktuellen Zeitpunkt gefunden"},
        500: {"description": "Fehler beim Abruf oder bei der Verarbeitung"},
    },
)
def get_current(
    horizon: int = Query(96, description="Zeithorizont in Stunden (48 oder 96)."),
    resolution: Literal["hourly", "quarter_hourly"] = Query("hourly", description="Zeitauflösung"),
    token: str = Query(..., description="API-Token für energyforecast.de"),
    fixed_net_cost: float = Query(0.0, description="Netzgebühren in EUR/kWh (z. B. 0.08)"),
    markup_costs: float = Query(0.0, description="Anbieter-Aufschlag in EUR/kWh (z. B. 0.01 für 1 ct/kWh)"),
    fixed_cost_other: float = Query(0.0, description="Sonstige Fixkosten in EUR/kWh"),
    vat: float = Query(0.19, description="Mehrwertsteuer (0.19 oder 19)."),
    cache_ttl_minutes: int = Query(60, ge=1, le=24 * 60, description="Cache-Gültigkeit in Minuten"),
    resultformat: Literal["default", "evcc"] = Query(
        "default", description="Ausgabeformat: 'default' oder 'evcc'"
    ),
    price_cap: Optional[float] = Query(
        None, description="Preisobergrenze in EUR/kWh nach Steuern und Gebühren."
    ),
    tz: Optional[str] = Query(
        None, description="Ausgabe-Zeitzone für Zeitstempel (z. B. 'Europe/Berlin', 'UTC'). Standard: UTC für default-Format, Original-Offset für evcc."
    ),
    dyn_net_operator: Optional[str] = Query(
        None, description="§14a EnWG Netzbetreiber-ID (z. B. 'syna'). Aktiviert zeitvariable Netzgebühren, überschreibt fixed_net_cost."
    ),
    dyn_net_country: str = Query("de", description="Ländercode für die dynamic_energy_fees API (Standard: de)"),
    dyn_net_api_url: str = Query(DYN_NET_API_URL_DEFAULT, description="Basis-URL der dynamic_energy_fees API"),
):
    """
    Gibt den Strompreis für den aktuellen Zeitpunkt zurück.
    Greift auf denselben Cache wie /prices zurück.

    Antwort:
    { "start": "...Z", "end": "...Z", "value": <EUR/kWh> }
    """
    try:
        _request_counts["current"] += 1
        if tz is not None:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                raise HTTPException(status_code=400, detail=f"Unbekannte Zeitzone: '{tz}'")
        if horizon not in (48, 96):
            raise HTTPException(status_code=400, detail="horizon must be 48 or 96")

        api_url = _endpoint_for_horizon(horizon)
        resolution_api = _res_to_api(resolution)
        output_tz = tz if tz is not None else ("UTC" if resultformat == "default" else None)

        raw_prices = _get_prices_cached(
            api_url, resolution_api, token, output_tz, cache_ttl_minutes,
            skip_cache_read=False,
        )
        prices = _select_costs(
            raw_prices, fixed_net_cost, markup_costs, fixed_cost_other, vat,
            dyn_net_operator, dyn_net_country, dyn_net_api_url, horizon, cache_ttl_minutes,
        )

        if price_cap is not None:
            prices = [{**p, "value": min(p["value"], price_cap)} for p in prices]

        now = datetime.now(timezone.utc)
        for entry in prices:
            s = entry["start"]
            e = entry["end"]
            start_dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
            end_dt = datetime.fromisoformat(e[:-1] + "+00:00" if e.endswith("Z") else e)
            if start_dt <= now < end_dt:
                logger.info(f"Current price: {entry['value']} EUR/kWh at {now.isoformat()}")
                return entry

        raise HTTPException(status_code=404, detail="Kein Preis für den aktuellen Zeitpunkt gefunden")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in get_current: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _simple_config_or_503():
    """Gibt die Simple-Konfiguration zurück oder wirft 503, wenn nicht konfiguriert."""
    if not _SIMPLE_CONFIG:
        raise HTTPException(
            status_code=503,
            detail="Simple-Modus nicht aktiv. Bitte ENERGYFORECAST_TOKEN (und optional weitere ENERGYFORECAST_*-Variablen) setzen."
        )
    return _SIMPLE_CONFIG


def _apply_simple_request(cfg: dict) -> List[dict]:
    """Führt den Abruf mit Simple-Konfiguration durch."""
    api_url = _endpoint_for_horizon(cfg["horizon"])
    resolution_api = _res_to_api(cfg["resolution"])
    resultformat = cfg["resultformat"]
    output_tz = cfg["tz"] if cfg["tz"] is not None else ("UTC" if resultformat == "default" else None)

    raw_prices = _get_prices_cached(
        api_url, resolution_api, cfg["token"],
        output_tz, cfg["cache_ttl_minutes"],
        skip_cache_read=_in_no_cache_window(),
    )
    prices = _select_costs(
        raw_prices,
        cfg["fixed_net_cost"], cfg["markup_costs"], cfg["fixed_cost_other"], cfg["vat"],
        cfg.get("dyn_net_operator"), cfg.get("dyn_net_country", "de"),
        cfg.get("dyn_net_api_url", DYN_NET_API_URL_DEFAULT),
        cfg["horizon"], cfg["cache_ttl_minutes"],
    )

    if cfg["price_cap"] is not None:
        prices = [{**p, "value": min(p["value"], cfg["price_cap"])} for p in prices]

    return prices, resultformat


@app.get("/simple/prices", tags=["simple"], response_model=Dict[str, List[dict]])
def simple_prices(
    resultformat: Optional[Literal["default", "evcc"]] = Query(None, description="Ausgabeformat überschreiben (Standard: ENERGYFORECAST_RESULT_FORMAT)"),
    tz: Optional[str] = Query(None, description="Ausgabe-Zeitzone überschreiben (Standard: ENERGYFORECAST_TZ)"),
):
    """
    Liefert alle Preise auf Basis der Umgebungsvariablen-Konfiguration.
    Kein Query-Parameter nötig – ideal für lokale Deployments.
    resultformat und tz können optional überschrieben werden.
    """
    try:
        cfg = _simple_config_or_503()
        if tz is not None:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                raise HTTPException(status_code=400, detail=f"Unbekannte Zeitzone: '{tz}'")
        effective_cfg = {**cfg}
        if resultformat is not None:
            effective_cfg["resultformat"] = resultformat
        if tz is not None:
            effective_cfg["tz"] = tz
        prices, fmt = _apply_simple_request(effective_cfg)
        logger.info(f"Simple /prices: {len(prices)} Einträge, format={fmt}")
        return {"rates": prices} if fmt == "evcc" else {"prices": prices}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Fehler in simple_prices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/simple/current", tags=["simple"], response_model=Dict[str, Any])
def simple_current(
    resultformat: Optional[Literal["default", "evcc"]] = Query(None, description="Ausgabeformat überschreiben (Standard: ENERGYFORECAST_RESULT_FORMAT)"),
    tz: Optional[str] = Query(None, description="Ausgabe-Zeitzone überschreiben (Standard: ENERGYFORECAST_TZ)"),
):
    """
    Liefert den aktuell gültigen Preis auf Basis der Umgebungsvariablen-Konfiguration.
    Trifft immer den Cache – kein direkter API-Aufruf außer bei Cache-Miss.
    resultformat und tz können optional überschrieben werden.
    """
    try:
        cfg = _simple_config_or_503()
        if tz is not None:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                raise HTTPException(status_code=400, detail=f"Unbekannte Zeitzone: '{tz}'")
        api_url = _endpoint_for_horizon(cfg["horizon"])
        resolution_api = _res_to_api(cfg["resolution"])
        resultformat = resultformat if resultformat is not None else cfg["resultformat"]
        effective_tz = tz if tz is not None else cfg["tz"]
        output_tz = effective_tz if effective_tz is not None else ("UTC" if resultformat == "default" else None)

        raw_prices = _get_prices_cached(
            api_url, resolution_api, cfg["token"],
            output_tz, cfg["cache_ttl_minutes"],
            skip_cache_read=False,
        )
        prices = _select_costs(
            raw_prices,
            cfg["fixed_net_cost"], cfg["markup_costs"], cfg["fixed_cost_other"], cfg["vat"],
            cfg.get("dyn_net_operator"), cfg.get("dyn_net_country", "de"),
            cfg.get("dyn_net_api_url", DYN_NET_API_URL_DEFAULT),
            cfg["horizon"], cfg["cache_ttl_minutes"],
        )

        if cfg["price_cap"] is not None:
            prices = [{**p, "value": min(p["value"], cfg["price_cap"])} for p in prices]

        now = datetime.now(timezone.utc)
        for entry in prices:
            s, e = entry["start"], entry["end"]
            start_dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
            end_dt = datetime.fromisoformat(e[:-1] + "+00:00" if e.endswith("Z") else e)
            if start_dt <= now < end_dt:
                logger.info(f"Simple /current: {entry['value']} EUR/kWh")
                return entry

        raise HTTPException(status_code=404, detail="Kein Preis für den aktuellen Zeitpunkt gefunden")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Fehler in simple_current: {e}")
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
    with _fee_cache_lock:
        fee_cached_entries = len(_fee_cache)
    return {
        "status": "ok",
        "service": APP_TITLE,
        "cached_entries": cached_entries,
        "cache_hits": _cache_stats["hits"],
        "cache_misses": _cache_stats["misses"],
        "fee_cache_entries": fee_cached_entries,
        "fee_cache_hits": _fee_cache_stats["hits"],
        "fee_cache_misses": _fee_cache_stats["misses"],
        "no_cache_window_active": _in_no_cache_window(),
        "request_counts": dict(_request_counts),
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
