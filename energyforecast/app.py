import os
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import List, Literal, Dict, Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_DATEFMT = '%Y-%m-%d %H:%M:%S'

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT
)
logger = logging.getLogger(__name__)

for logger_name in ['uvicorn', 'uvicorn.access', 'uvicorn.error']:
    uvicorn_logger = logging.getLogger(logger_name)
    uvicorn_logger.handlers.clear()
    handler = logging.StreamHandler()
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    handler.setFormatter(formatter)
    uvicorn_logger.addHandler(handler)
    uvicorn_logger.setLevel(logging.INFO)
    uvicorn_logger.propagate = False

APP_TITLE = "Energyforecast.de Proxy (API v2)"
API_BASE = "https://www.energyforecast.de/api/v2/forecast"
DYN_NET_API_URL_DEFAULT = "https://dyn-net.batcontrol.software/api"

# DE und LU werden beide durch die DE-LU-Marktzone bedient
_MARKET_ZONE_ALIASES = {'DE': 'DE-LU', 'LU': 'DE-LU'}

app = FastAPI(title=APP_TITLE)


# ---------- Simple-Modus: Konfiguration aus Umgebungsvariablen ----------
def _load_simple_config() -> Optional[dict]:
    """
    Liest Konfiguration fuer den Simple-Modus aus Umgebungsvariablen.
    Gibt None zurueck, wenn ENERGYFORECAST_TOKEN nicht gesetzt ist.

    Variablen:
      ENERGYFORECAST_TOKEN              API-Token (Pflicht fuer Simple-Modus)
      ENERGYFORECAST_MARKET_ZONE        Marktzone, z. B. DE-LU (Standard: DE-LU)
      ENERGYFORECAST_HORIZON            Max. Stunden zurueckgeben, 0=alle (Standard: 0)
      ENERGYFORECAST_RESOLUTION         hourly | quarter_hourly (Standard: hourly)
      ENERGYFORECAST_USE_TOTAL_PRICE    true/false: API-berechneten Gesamtpreis nutzen (Standard: false)
      ENERGYFORECAST_FIXED_NET_COST     Netzgebuehren in EUR/kWh, z. B. 0.08 (Standard: 0.0)
      ENERGYFORECAST_MARKUP_COSTS       Anbieter-Aufschlag in EUR/kWh, z. B. 0.01 (Standard: 0.0)
      ENERGYFORECAST_FIXED_COST_OTHER   Sonstige Fixkosten in EUR/kWh (Standard: 0.0)
      ENERGYFORECAST_VAT                z. B. 0.19 oder 19 (Standard: 0.19)
      ENERGYFORECAST_PRICE_CAP          EUR/kWh, optional
      ENERGYFORECAST_CACHE_TTL          Minuten (Standard: 60)
      ENERGYFORECAST_RESULT_FORMAT      default | evcc (Standard: default)
      ENERGYFORECAST_TZ                 IANA-Zeitzone, z. B. Europe/Berlin (optional)
      ENERGYFORECAST_DYN_NET_OPERATOR   14a Netzbetreiber-ID, z. B. syna (optional)
      ENERGYFORECAST_DYN_NET_COUNTRY    Laendercode fuer Netzgebuehren-API (Standard: de)
      ENERGYFORECAST_DYN_NET_API_URL    Basis-URL der dynamic_energy_fees API
    """
    token = os.getenv("ENERGYFORECAST_TOKEN")
    if not token:
        return None

    price_cap_raw = os.getenv("ENERGYFORECAST_PRICE_CAP")
    tz_raw = os.getenv("ENERGYFORECAST_TZ")
    horizon_raw = int(os.getenv("ENERGYFORECAST_HORIZON", "0"))
    use_total_price_raw = os.getenv("ENERGYFORECAST_USE_TOTAL_PRICE", "false").lower()
    market_zone_raw = os.getenv("ENERGYFORECAST_MARKET_ZONE", "DE-LU")

    return {
        "token": token,
        "market_zone": _normalize_market_zone(market_zone_raw),
        "horizon": horizon_raw if horizon_raw > 0 else None,
        "resolution": os.getenv("ENERGYFORECAST_RESOLUTION", "hourly"),
        "use_total_price": use_total_price_raw in ("1", "true", "yes"),
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
        horizon_str = "alle" if _SIMPLE_CONFIG["horizon"] is None else f"{_SIMPLE_CONFIG['horizon']}h"
        logger.info(
            f"Simple-Modus aktiv: zone={_SIMPLE_CONFIG['market_zone']}, "
            f"horizon={horizon_str}, "
            f"resolution={_SIMPLE_CONFIG['resolution']}, "
            f"use_total_price={_SIMPLE_CONFIG['use_total_price']}, "
            f"fixed_net_cost={_SIMPLE_CONFIG['fixed_net_cost']}, "
            f"markup_costs={_SIMPLE_CONFIG['markup_costs']}, "
            f"vat={_SIMPLE_CONFIG['vat']}, "
            f"format={_SIMPLE_CONFIG['resultformat']}, "
            f"tz={_SIMPLE_CONFIG['tz']}"
        )
        if _SIMPLE_CONFIG.get("dyn_net_operator"):
            logger.info(
                f"Dynamische Netzgebuehren aktiv: operator={_SIMPLE_CONFIG['dyn_net_operator']}, "
                f"country={_SIMPLE_CONFIG['dyn_net_country']}, "
                f"api_url={_SIMPLE_CONFIG['dyn_net_api_url']}"
            )
        else:
            logger.info("Dynamische Netzgebuehren inaktiv (ENERGYFORECAST_DYN_NET_OPERATOR nicht gesetzt)")
    else:
        logger.info("Simple-Modus inaktiv (ENERGYFORECAST_TOKEN nicht gesetzt)")


# ---------- Hilfsfunktionen ----------
def _normalize_market_zone(zone: str) -> str:
    """Normalisiert Marktzonenkuerzel auf den API-gueltigen Wert."""
    z = zone.strip().upper()
    return _MARKET_ZONE_ALIASES.get(z, z)


def _convert_timestamp(dt_str: str, tz_name: Optional[str]) -> str:
    """
    Konvertiert einen ISO-Zeitstempel in die gewuenschte Zeitzone.
    tz_name=None  -> Zeitstempel unveraendert zurueckgeben
    tz_name="UTC" -> UTC mit Z-Suffix (z. B. 2024-01-01T13:00:00Z)
    tz_name=...   -> Lokale Zeit mit Offset (z. B. 2024-01-01T14:00:00+01:00)
    """
    if tz_name is None:
        return dt_str
    dt = (
        datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        .astimezone(ZoneInfo(tz_name))
        .replace(microsecond=0)
    )
    iso = dt.isoformat()
    return iso.replace("+00:00", "Z") if tz_name == "UTC" else iso


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
    vat kann als Faktor (0.19) oder Prozent (19) uebergeben werden.
    """
    vat_factor = (vat / 100.0) if vat > 1.0 else vat
    total_fixed = fixed_net_cost + markup_costs + fixed_cost_other
    return [
        {**p, "value": round((p["value"] + total_fixed) * (1 + vat_factor), 6)}
        for p in raw_prices
    ]


def _ttl_bucket(ttl_minutes: int) -> int:
    """Erzeugt groben Zeit-Bucket fuer Cache-Invalidierung."""
    seconds = max(1, int(ttl_minutes) * 60)
    return int(time.time() // seconds)


def _aggregate_to_hourly(slots: List[dict]) -> List[dict]:
    """
    Aggregiert Viertelstunden-Slots zu Stunden-Slots.
    Gruppiert nach Kalender-Stunde des Startzeitstempels, mittelt die Werte.
    Behaelt den Startzeitstempel des ersten und den Endzeitstempel des letzten Slots.
    """
    if not slots:
        return []
    groups: Dict[str, List[dict]] = {}
    order: List[str] = []
    for slot in slots:
        s = slot['start']
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        hour_key = dt.replace(minute=0, second=0, microsecond=0).isoformat()
        if hour_key not in groups:
            groups[hour_key] = []
            order.append(hour_key)
        groups[hour_key].append(slot)
    result = []
    for hour_key in order:
        group = groups[hour_key]
        avg = sum(s['value'] for s in group) / len(group)
        result.append({
            'start': group[0]['start'],
            'end': group[-1]['end'],
            'value': round(avg, 6),
        })
    return result


def _filter_horizon(prices: List[dict], horizon_hours: Optional[int]) -> List[dict]:
    """Behaelt nur Slots, deren Startzeitpunkt innerhalb der naechsten horizon_hours liegt."""
    if horizon_hours is None:
        return prices
    cutoff = datetime.now(timezone.utc) + timedelta(hours=horizon_hours)
    result = []
    for p in prices:
        s = p['start']
        dt = datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)
        if dt < cutoff:
            result.append(p)
    return result


# ---------- Cache-Infrastruktur ----------
# Struktur eines Eintrags: {"data": [...], "bucket": int}
_price_cache: Dict[tuple, dict] = {}
_cache_lock = threading.Lock()
_cache_stats = {"hits": 0, "misses": 0}
_request_counts: Dict[str, int] = {"prices": 0, "current": 0}

_fee_cache: Dict[tuple, dict] = {}
_fee_cache_lock = threading.Lock()
_fee_cache_stats = {"hits": 0, "misses": 0}


def _in_no_cache_window() -> bool:
    """True zwischen 12:00:00 UTC und 13:29:59 UTC (Veroeffentlichungsfenster Folgetagspreise)."""
    now = datetime.now(timezone.utc)
    return (now.hour == 12) or (now.hour == 13 and now.minute < 30)


# ---------- Dynamische Netzgebuehren ----------
def _fetch_net_fees(api_url: str, operator: str, country: str, next_hours: int) -> List[dict]:
    """Holt zeitvariable Netzgebuehren-Slots von der dynamic_energy_fees-API."""
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
    """Gibt gecachte Fee-Slots zurueck. Wirft RuntimeError bei API-Fehler (kein Fallback)."""
    key = (api_url, operator, country, next_hours)
    now_bucket = _ttl_bucket(ttl_minutes)
    with _fee_cache_lock:
        entry = _fee_cache.get(key)
    if entry is not None and entry["bucket"] == now_bucket:
        _fee_cache_stats["hits"] += 1
        return entry["data"]
    _fee_cache_stats["misses"] += 1
    data = _fetch_net_fees(api_url, operator, country, next_hours)
    with _fee_cache_lock:
        _fee_cache[key] = {"data": data, "bucket": now_bucket}
    return data


def _resolve_net_cost_for_slot(price_start_str: str, fee_slots: List[dict], fallback: float = 0.0) -> float:
    """Sucht den passenden Fee-Slot fuer einen Preis-Slot-Startzeitpunkt."""
    s = price_start_str
    price_start = datetime.fromisoformat(
        s.replace('Z', '+00:00')
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
    """Wie _apply_costs(), aber mit zeitslot-genauer Netzgebuehr aus fee_slots."""
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
    next_hours: int,
    cache_ttl_minutes: int,
) -> List[dict]:
    """Wendet statische oder dynamische Netzgebuehren an."""
    if dyn_net_operator:
        fee_slots = _get_net_fees_cached(
            dyn_net_api_url, dyn_net_operator, dyn_net_country, next_hours, cache_ttl_minutes
        )
        return _apply_costs_dynamic(raw_prices, fee_slots, markup_costs, fixed_cost_other, vat)
    return _apply_costs(raw_prices, fixed_net_cost, markup_costs, fixed_cost_other, vat)


# ---------- API Fetch (ohne Cache) ----------
def _fetch_prices_from_api(
    token: str,
    market_zone: str,
    output_tz: Optional[str] = "UTC",
    use_total_price: bool = False,
) -> List[dict]:
    """
    Holt Viertelstunden-Spotpreise direkt von der energyforecast.de API v2 (kein Cache).

    API v2 liefert immer Viertelstunden-Daten fuer vollstaendige Kalendertage.
    Response-Format:
      {
        "generated_at": "...",
        "valid_until": "...",
        "data": [
          {"start": "...", "end": "...", "price_ct_kwh": 12.34, "total_ct_kwh": 27.89, ...}
        ]
      }

    use_total_price=False (Standard): price_ct_kwh / 100 -> EUR/kWh (Spot, ohne Gebuehren).
    use_total_price=True:             total_ct_kwh / 100 -> EUR/kWh (inkl. API-seitig berechneter Gebuehren+MwSt.).
    """
    params: dict = {'token': token, 'market_zone': market_zone}
    if not use_total_price:
        # Serverseitige Berechnung unterdruecken; Kosten werden lokal angewendet.
        params['vat'] = 0
        params['fixed_cost_cent'] = 0

    logger.info(
        f"Fetching from API v2: zone={market_zone}, "
        f"mode={'total_ct_kwh' if use_total_price else 'price_ct_kwh'}"
    )
    try:
        resp = requests.get(API_BASE, params=params, timeout=15)
    except requests.RequestException as e:
        logger.error(f"Upstream request failed: {e}")
        raise RuntimeError(f"Upstream request failed: {e}") from e

    if resp.status_code != 200:
        logger.error(f"Upstream returned {resp.status_code}: {resp.text[:300]}")
        raise RuntimeError(f"Upstream returned {resp.status_code}: {resp.text[:300]}")

    try:
        body = resp.json()
        raw_data = body.get('data')
        if not isinstance(raw_data, list):
            raise ValueError("Unexpected JSON shape: missing 'data' list.")
    except (ValueError, AttributeError) as e:
        logger.error(f"Invalid JSON from upstream: {e}")
        raise RuntimeError(f"Invalid JSON from upstream: {e}") from e

    price_field = 'total_ct_kwh' if use_total_price else 'price_ct_kwh'
    out = []
    for item in raw_data:
        start = _convert_timestamp(item['start'], output_tz)
        end = _convert_timestamp(item['end'], output_tz)
        value = item[price_field] / 100  # ct/kWh -> EUR/kWh
        out.append({'start': start, 'end': end, 'value': value})

    logger.info(f"Fetched {len(out)} quarter-hourly slots from API v2")
    return out


# ---------- Cache-bewusster Abruf ----------
def _get_prices_cached(
    token: str,
    market_zone: str,
    output_tz: Optional[str],
    ttl_minutes: int,
    use_total_price: bool = False,
    skip_cache_read: bool = False,
) -> List[dict]:
    """
    Gibt gecachte Viertelstunden-Spotpreise zurueck oder holt neue Daten von der API.
    Der Cache speichert Rohdaten (Spot) in Viertelstunden-Aufloesung.
    Resolution-Aggregation und Horizon-Filterung erfolgen nach dem Cache-Zugriff.

    skip_cache_read=True -> Cache-Lesen ueberspringen, frisch von der API holen,
                           Ergebnis trotzdem in den Cache schreiben.
    """
    key = (token, market_zone, output_tz, use_total_price)
    now_bucket = _ttl_bucket(ttl_minutes)

    if not skip_cache_read:
        with _cache_lock:
            entry = _price_cache.get(key)
        if entry is not None and entry['bucket'] == now_bucket:
            _cache_stats['hits'] += 1
            return entry['data']
    else:
        logger.info("No-cache window aktiv (12:00-13:30 UTC) - Cache-Lesen uebersprungen")

    _cache_stats['misses'] += 1
    data = _fetch_prices_from_api(token, market_zone, output_tz, use_total_price)

    with _cache_lock:
        _price_cache[key] = {'data': data, 'bucket': now_bucket}

    return data


def _build_prices(
    raw_prices: List[dict],
    resolution: str,
    use_total_price: bool,
    fixed_net_cost: float,
    markup_costs: float,
    fixed_cost_other: float,
    vat: float,
    dyn_net_operator: Optional[str],
    dyn_net_country: str,
    dyn_net_api_url: str,
    cache_ttl_minutes: int,
    price_cap: Optional[float],
    horizon: Optional[int],
) -> List[dict]:
    """
    Gemeinsame Pipeline nach dem Cache-Zugriff:
      1. Kosten anwenden (nur wenn nicht use_total_price)
      2. Preisobergrenze
      3. Aggregation zu Stunden (wenn resolution=hourly)
      4. Horizon-Filter
    """
    if use_total_price:
        prices = list(raw_prices)
    else:
        next_hours = horizon if horizon else 120
        prices = _select_costs(
            raw_prices, fixed_net_cost, markup_costs, fixed_cost_other, vat,
            dyn_net_operator, dyn_net_country, dyn_net_api_url,
            next_hours, cache_ttl_minutes,
        )

    if price_cap is not None:
        prices = [{**p, "value": min(p["value"], price_cap)} for p in prices]
        logger.info(f"Applied price_cap={price_cap} EUR/kWh")

    if resolution == "hourly":
        prices = _aggregate_to_hourly(prices)

    prices = _filter_horizon(prices, horizon)

    return prices


# ---------- Hauptendpunkte ----------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """Nutzungshinweis fuer den Proxy."""
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
    .badge { background: #e8f4fd; border: 1px solid #b3d4eb; border-radius: 3px; padding: 1px 6px; font-size: 0.8em; color: #1a6496; }
  </style>
</head>
<body>
  <h1>Energyforecast.de Proxy <span class="badge">API v2</span></h1>
  <p>Proxy fuer die <a href="https://www.energyforecast.de">energyforecast.de</a> API v2.
     Liefert Strompreis-Vorhersagen mit optionalem Fixkosten-Aufschlag, MwSt. und Preisobergrenze.
     &nbsp;&middot;&nbsp; <a href="https://github.com/MaStr/energyforecast_proxy">GitHub</a></p>

  <p><strong>API v2 liefert immer Viertelstunden-Daten</strong> fuer vollstaendige Kalendertage.
     Der Proxy aggregiert optional auf Stunden (<code>resolution=hourly</code>)
     und filtert optional auf einen Zeithorizont (<code>horizon</code>).</p>

  <h2>Endpunkte</h2>
  <table>
    <tr><th>Pfad</th><th>Beschreibung</th></tr>
    <tr><td><code>GET /prices</code></td><td>Strompreis-Vorhersage (alle Parameter als Query-Parameter)</td></tr>
    <tr><td><code>GET /current</code></td><td>Aktuellen Preis abrufen</td></tr>
    <tr><td><code>GET /simple/prices</code></td><td>Alle Preise – Konfiguration aus Umgebungsvariablen</td></tr>
    <tr><td><code>GET /simple/current</code></td><td>Aktueller Preis – Konfiguration aus Umgebungsvariablen</td></tr>
    <tr><td><code>GET /health</code></td><td>Dienststatus und Cache-Statistiken</td></tr>
  </table>

  <h2>Simple-Modus (lokales Deployment)</h2>
  <p>Wenn <code>ENERGYFORECAST_TOKEN</code> gesetzt ist, sind <code>/simple/prices</code> und
     <code>/simple/current</code> ohne Query-Parameter nutzbar:</p>
  <table>
    <tr><th>Umgebungsvariable</th><th>Standard</th><th>Beschreibung</th></tr>
    <tr><td><code>ENERGYFORECAST_TOKEN</code></td><td><em>Pflicht</em></td><td>API-Token fuer energyforecast.de</td></tr>
    <tr><td><code>ENERGYFORECAST_MARKET_ZONE</code></td><td>DE-LU</td><td>Marktzone (DE und LU werden automatisch zu DE-LU normalisiert)</td></tr>
    <tr><td><code>ENERGYFORECAST_HORIZON</code></td><td>0 (alle)</td><td>Max. Stunden zurueckgeben; 0 = alle verfuegbaren Daten</td></tr>
    <tr><td><code>ENERGYFORECAST_RESOLUTION</code></td><td>hourly</td><td><code>hourly</code> (Aggregation zu Stunden) oder <code>quarter_hourly</code> (native 15-min)</td></tr>
    <tr><td><code>ENERGYFORECAST_USE_TOTAL_PRICE</code></td><td>false</td><td>true: API-seitig berechneten Gesamtpreis (inkl. Gebuehren+MwSt.) verwenden; ueberschreibt alle lokalen Kostenparameter</td></tr>
    <tr><td><code>ENERGYFORECAST_FIXED_NET_COST</code></td><td>0.0</td><td>Netzgebuehren in EUR/kWh</td></tr>
    <tr><td><code>ENERGYFORECAST_MARKUP_COSTS</code></td><td>0.0</td><td>Anbieter-Aufschlag in EUR/kWh</td></tr>
    <tr><td><code>ENERGYFORECAST_FIXED_COST_OTHER</code></td><td>0.0</td><td>Sonstige Fixkosten in EUR/kWh</td></tr>
    <tr><td><code>ENERGYFORECAST_VAT</code></td><td>0.19</td><td>Mehrwertsteuer (0.19 oder 19)</td></tr>
    <tr><td><code>ENERGYFORECAST_PRICE_CAP</code></td><td>&ndash;</td><td>Preisobergrenze in EUR/kWh (optional)</td></tr>
    <tr><td><code>ENERGYFORECAST_CACHE_TTL</code></td><td>60</td><td>Cache-Gueltigkeit in Minuten</td></tr>
    <tr><td><code>ENERGYFORECAST_RESULT_FORMAT</code></td><td>default</td><td><code>default</code> oder <code>evcc</code></td></tr>
    <tr><td><code>ENERGYFORECAST_TZ</code></td><td>&ndash;</td><td>Ausgabe-Zeitzone, z.&nbsp;B. <code>Europe/Berlin</code> (optional)</td></tr>
    <tr><td><code>ENERGYFORECAST_DYN_NET_OPERATOR</code></td><td>&ndash;</td><td>14a EnWG Netzbetreiber-ID (z.&nbsp;B. <code>syna</code>). Aktiviert zeitvariable Netzgebuehren, ueberschreibt <code>ENERGYFORECAST_FIXED_NET_COST</code>.</td></tr>
    <tr><td><code>ENERGYFORECAST_DYN_NET_COUNTRY</code></td><td>de</td><td>Laendercode fuer die Netzgebuehren-API</td></tr>
    <tr><td><code>ENERGYFORECAST_DYN_NET_API_URL</code></td><td><code>https://dyn-net.batcontrol.software/api</code></td><td>Nur bei Self-Hosting der dynamic_energy_fees-Instanz noetig</td></tr>
  </table>

  <h2>Parameter <code>/prices</code></h2>
  <table>
    <tr><th>Parameter</th><th>Typ</th><th>Standard</th><th>Beschreibung</th></tr>
    <tr><td><code>token</code></td><td>string</td><td><em>Pflicht</em></td><td>API-Token fuer energyforecast.de</td></tr>
    <tr><td><code>market_zone</code></td><td>string</td><td>DE-LU</td><td>Marktzone, z.&nbsp;B. <code>DE-LU</code>, <code>AT</code>, <code>FR</code>. DE und LU werden automatisch zu DE-LU normalisiert.</td></tr>
    <tr><td><code>horizon</code></td><td>int</td><td>&ndash; (alle)</td><td>Zeithorizont in Stunden als Post-Fetch-Filter. Nicht gesetzt = alle verfuegbaren Daten zurueckgeben.</td></tr>
    <tr><td><code>resolution</code></td><td>string</td><td>hourly</td><td><code>hourly</code> (Aggregation zu Stunden) oder <code>quarter_hourly</code> (native 15-min der API v2)</td></tr>
    <tr><td><code>use_total_price</code></td><td>bool</td><td>false</td><td>true: API-seitig berechneten <code>total_ct_kwh</code> verwenden (inkl. dyn. Netzgebuehren+MwSt.). Alle lokalen Kostenparameter werden ignoriert.</td></tr>
    <tr><td><code>fixed_net_cost</code></td><td>float</td><td>0.0</td><td>Netzgebuehren in EUR/kWh</td></tr>
    <tr><td><code>markup_costs</code></td><td>float</td><td>0.0</td><td>Anbieter-Aufschlag in EUR/kWh</td></tr>
    <tr><td><code>fixed_cost_other</code></td><td>float</td><td>0.0</td><td>Sonstige Fixkosten in EUR/kWh</td></tr>
    <tr><td><code>vat</code></td><td>float</td><td>0.19</td><td>Mehrwertsteuer als Faktor (<code>0.19</code>) oder Prozent (<code>19</code>)</td></tr>
    <tr><td><code>price_cap</code></td><td>float</td><td>&ndash;</td><td>Preisobergrenze in EUR/kWh nach Steuern und Gebuehren</td></tr>
    <tr><td><code>cache_ttl_minutes</code></td><td>int</td><td>60</td><td>Cache-Gueltigkeit in Minuten (1&ndash;1440)</td></tr>
    <tr><td><code>resultformat</code></td><td>string</td><td>default</td><td>Ausgabeformat &ndash; siehe unten</td></tr>
    <tr><td><code>tz</code></td><td>string</td><td>&ndash;</td><td>Ausgabe-Zeitzone (IANA-Name). Standard: UTC bei <code>default</code>-Format, Original-Offset der API bei <code>evcc</code>.</td></tr>
    <tr><td><code>dyn_net_operator</code></td><td>string</td><td>&ndash;</td><td>14a EnWG Netzbetreiber-ID. Aktiviert zeitvariable Netzgebuehren, ueberschreibt <code>fixed_net_cost</code>.</td></tr>
    <tr><td><code>dyn_net_country</code></td><td>string</td><td>de</td><td>Laendercode fuer die dynamic_energy_fees API</td></tr>
    <tr><td><code>dyn_net_api_url</code></td><td>string</td><td><em>Default-URL</em></td><td>Basis-URL der dynamic_energy_fees API</td></tr>
  </table>

  <h2>Ausgabeformat: <code>resultformat</code></h2>
  <p><strong><code>default</code></strong> &ndash; Zeitstempel nach UTC, Schluessel <code>prices</code>:</p>
  <pre>{ "prices": [ { "start": "2024-01-01T12:00:00Z", "end": "2024-01-01T13:00:00Z", "value": 0.2843 } ] }</pre>

  <p><strong><code>evcc</code></strong> &ndash; Zeitstempel im Original-Format der API (mit Zeitzonenoffset), Schluessel <code>rates</code>:</p>
  <pre>{ "rates": [ { "start": "2024-01-01T13:00:00+01:00", "end": "2024-01-01T14:00:00+01:00", "value": 0.2843 } ] }</pre>

  <div class="note">
    <code>value</code> ist immer der Endpreis in <strong>EUR/kWh</strong>
    (Spot-Preis + Gebuehren + MwSt., ggf. gedeckelt durch <code>price_cap</code>).
  </div>

  <h2>Beispielaufruf</h2>
  <pre>GET /prices?token=DEIN_TOKEN&amp;market_zone=DE-LU&amp;fixed_net_cost=0.08&amp;markup_costs=0.01&amp;vat=0.19&amp;resultformat=evcc</pre>
</body>
</html>"""


@app.get(
    "/prices",
    response_model=Dict[str, List[dict]],
    responses={
        200: {"description": "OK"},
        500: {"description": "Fehler beim Abruf oder bei der Verarbeitung"},
    },
)
def get_prices(
    token: str = Query(..., description="API-Token fuer energyforecast.de"),
    market_zone: str = Query("DE-LU", description="Marktzone, z. B. DE-LU, AT, FR. DE/LU werden automatisch zu DE-LU normalisiert."),
    horizon: Optional[int] = Query(None, description="Zeithorizont in Stunden als Post-Fetch-Filter. Nicht gesetzt = alle verfuegbaren Daten."),
    resolution: Literal["hourly", "quarter_hourly"] = Query("hourly", description="Zeitaufloesung: hourly (Stunden-Aggregation) oder quarter_hourly (native 15-min)"),
    use_total_price: bool = Query(False, description="true: API-seitig berechneten Gesamtpreis (total_ct_kwh) nutzen; ignoriert alle lokalen Kostenparameter"),
    fixed_net_cost: float = Query(0.0, description="Netzgebuehren in EUR/kWh"),
    markup_costs: float = Query(0.0, description="Anbieter-Aufschlag in EUR/kWh"),
    fixed_cost_other: float = Query(0.0, description="Sonstige Fixkosten in EUR/kWh"),
    vat: float = Query(0.19, description="Mehrwertsteuer (0.19 oder 19)"),
    cache_ttl_minutes: int = Query(60, ge=1, le=24 * 60, description="Cache-Gueltigkeit in Minuten"),
    resultformat: Literal["default", "evcc"] = Query("default", description="Ausgabeformat: 'default' fuer {prices: [...]}, 'evcc' fuer {rates: [...]}"),
    price_cap: Optional[float] = Query(None, description="Preisobergrenze in EUR/kWh nach Steuern und Gebuehren"),
    tz: Optional[str] = Query(None, description="Ausgabe-Zeitzone (z. B. 'Europe/Berlin', 'UTC'). Standard: UTC fuer default-Format, Original-Offset fuer evcc."),
    dyn_net_operator: Optional[str] = Query(None, description="14a EnWG Netzbetreiber-ID (z. B. 'syna'). Aktiviert zeitvariable Netzgebuehren."),
    dyn_net_country: str = Query("de", description="Laendercode fuer die dynamic_energy_fees API"),
    dyn_net_api_url: str = Query(DYN_NET_API_URL_DEFAULT, description="Basis-URL der dynamic_energy_fees API"),
):
    try:
        if tz is not None:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                raise HTTPException(status_code=400, detail=f"Unbekannte Zeitzone: '{tz}'")

        _request_counts["prices"] += 1
        normalized_zone = _normalize_market_zone(market_zone)
        output_tz = tz if tz is not None else ("UTC" if resultformat == "default" else None)

        logger.info(
            f"Request /prices: zone={normalized_zone}, resolution={resolution}, "
            f"horizon={horizon}, use_total_price={use_total_price}"
        )

        raw_prices = _get_prices_cached(
            token, normalized_zone, output_tz, cache_ttl_minutes,
            use_total_price=use_total_price,
            skip_cache_read=_in_no_cache_window(),
        )

        prices = _build_prices(
            raw_prices, resolution, use_total_price,
            fixed_net_cost, markup_costs, fixed_cost_other, vat,
            dyn_net_operator, dyn_net_country, dyn_net_api_url,
            cache_ttl_minutes, price_cap, horizon,
        )

        logger.info(f"Returning {len(prices)} slots, format={resultformat}")

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
        404: {"description": "Kein Preis fuer den aktuellen Zeitpunkt gefunden"},
        500: {"description": "Fehler beim Abruf oder bei der Verarbeitung"},
    },
)
def get_current(
    token: str = Query(..., description="API-Token fuer energyforecast.de"),
    market_zone: str = Query("DE-LU", description="Marktzone, z. B. DE-LU, AT, FR"),
    resolution: Literal["hourly", "quarter_hourly"] = Query("hourly", description="Zeitaufloesung"),
    use_total_price: bool = Query(False, description="true: API-seitig berechneten Gesamtpreis nutzen"),
    fixed_net_cost: float = Query(0.0, description="Netzgebuehren in EUR/kWh"),
    markup_costs: float = Query(0.0, description="Anbieter-Aufschlag in EUR/kWh"),
    fixed_cost_other: float = Query(0.0, description="Sonstige Fixkosten in EUR/kWh"),
    vat: float = Query(0.19, description="Mehrwertsteuer (0.19 oder 19)"),
    cache_ttl_minutes: int = Query(60, ge=1, le=24 * 60, description="Cache-Gueltigkeit in Minuten"),
    resultformat: Literal["default", "evcc"] = Query("default", description="Ausgabeformat"),
    price_cap: Optional[float] = Query(None, description="Preisobergrenze in EUR/kWh"),
    tz: Optional[str] = Query(None, description="Ausgabe-Zeitzone"),
    dyn_net_operator: Optional[str] = Query(None, description="14a EnWG Netzbetreiber-ID"),
    dyn_net_country: str = Query("de", description="Laendercode fuer die dynamic_energy_fees API"),
    dyn_net_api_url: str = Query(DYN_NET_API_URL_DEFAULT, description="Basis-URL der dynamic_energy_fees API"),
):
    """Gibt den Strompreis fuer den aktuellen Zeitpunkt zurueck."""
    try:
        if tz is not None:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                raise HTTPException(status_code=400, detail=f"Unbekannte Zeitzone: '{tz}'")

        _request_counts["current"] += 1
        normalized_zone = _normalize_market_zone(market_zone)
        output_tz = tz if tz is not None else ("UTC" if resultformat == "default" else None)

        raw_prices = _get_prices_cached(
            token, normalized_zone, output_tz, cache_ttl_minutes,
            use_total_price=use_total_price,
            skip_cache_read=False,
        )

        prices = _build_prices(
            raw_prices, resolution, use_total_price,
            fixed_net_cost, markup_costs, fixed_cost_other, vat,
            dyn_net_operator, dyn_net_country, dyn_net_api_url,
            cache_ttl_minutes, price_cap, horizon=None,
        )

        now = datetime.now(timezone.utc)
        for entry in prices:
            s = entry["start"]
            e = entry["end"]
            start_dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(e.replace('Z', '+00:00'))
            if start_dt <= now < end_dt:
                logger.info(f"Current price: {entry['value']} EUR/kWh at {now.isoformat()}")
                return entry

        raise HTTPException(status_code=404, detail="Kein Preis fuer den aktuellen Zeitpunkt gefunden")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error in get_current: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _simple_config_or_503():
    """Gibt die Simple-Konfiguration zurueck oder wirft 503, wenn nicht konfiguriert."""
    if not _SIMPLE_CONFIG:
        raise HTTPException(
            status_code=503,
            detail="Simple-Modus nicht aktiv. Bitte ENERGYFORECAST_TOKEN (und optional weitere ENERGYFORECAST_*-Variablen) setzen."
        )
    return _SIMPLE_CONFIG


def _apply_simple_request(cfg: dict) -> tuple:
    """Fuehrt den Abruf und die Preisberechnung mit Simple-Konfiguration durch."""
    resultformat = cfg["resultformat"]
    output_tz = cfg["tz"] if cfg["tz"] is not None else ("UTC" if resultformat == "default" else None)

    raw_prices = _get_prices_cached(
        cfg["token"], cfg["market_zone"], output_tz, cfg["cache_ttl_minutes"],
        use_total_price=cfg["use_total_price"],
        skip_cache_read=_in_no_cache_window(),
    )

    prices = _build_prices(
        raw_prices, cfg["resolution"], cfg["use_total_price"],
        cfg["fixed_net_cost"], cfg["markup_costs"], cfg["fixed_cost_other"], cfg["vat"],
        cfg.get("dyn_net_operator"), cfg.get("dyn_net_country", "de"),
        cfg.get("dyn_net_api_url", DYN_NET_API_URL_DEFAULT),
        cfg["cache_ttl_minutes"], cfg["price_cap"], cfg["horizon"],
    )

    return prices, resultformat


@app.get("/simple/prices", tags=["simple"], response_model=Dict[str, List[dict]])
def simple_prices(
    resultformat: Optional[Literal["default", "evcc"]] = Query(None, description="Ausgabeformat ueberschreiben"),
    tz: Optional[str] = Query(None, description="Ausgabe-Zeitzone ueberschreiben"),
):
    """
    Liefert alle Preise auf Basis der Umgebungsvariablen-Konfiguration.
    resultformat und tz koennen optional ueberschrieben werden.
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
        logger.info(f"Simple /prices: {len(prices)} Eintraege, format={fmt}")
        return {"rates": prices} if fmt == "evcc" else {"prices": prices}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Fehler in simple_prices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/simple/current", tags=["simple"], response_model=Dict[str, Any])
def simple_current(
    resultformat: Optional[Literal["default", "evcc"]] = Query(None, description="Ausgabeformat ueberschreiben"),
    tz: Optional[str] = Query(None, description="Ausgabe-Zeitzone ueberschreiben"),
):
    """
    Liefert den aktuell gueltigen Preis auf Basis der Umgebungsvariablen-Konfiguration.
    """
    try:
        cfg = _simple_config_or_503()
        if tz is not None:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                raise HTTPException(status_code=400, detail=f"Unbekannte Zeitzone: '{tz}'")

        effective_resultformat = resultformat if resultformat is not None else cfg["resultformat"]
        effective_tz = tz if tz is not None else cfg["tz"]
        output_tz = effective_tz if effective_tz is not None else ("UTC" if effective_resultformat == "default" else None)

        raw_prices = _get_prices_cached(
            cfg["token"], cfg["market_zone"], output_tz, cfg["cache_ttl_minutes"],
            use_total_price=cfg["use_total_price"],
            skip_cache_read=False,
        )

        prices = _build_prices(
            raw_prices, cfg["resolution"], cfg["use_total_price"],
            cfg["fixed_net_cost"], cfg["markup_costs"], cfg["fixed_cost_other"], cfg["vat"],
            cfg.get("dyn_net_operator"), cfg.get("dyn_net_country", "de"),
            cfg.get("dyn_net_api_url", DYN_NET_API_URL_DEFAULT),
            cfg["cache_ttl_minutes"], cfg["price_cap"], cfg["horizon"],
        )

        now = datetime.now(timezone.utc)
        for entry in prices:
            s, e = entry["start"], entry["end"]
            start_dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
            end_dt = datetime.fromisoformat(e.replace('Z', '+00:00'))
            if start_dt <= now < end_dt:
                logger.info(f"Simple /current: {entry['value']} EUR/kWh")
                return entry

        raise HTTPException(status_code=404, detail="Kein Preis fuer den aktuellen Zeitpunkt gefunden")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Fehler in simple_current: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health", tags=["system"])
def health() -> Dict[str, Any]:
    """Healthcheck mit Cache-Statistiken."""
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
