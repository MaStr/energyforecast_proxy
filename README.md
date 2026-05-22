# Energyforecast Proxy

FastAPI-Proxy für die [energyforecast.de](https://www.energyforecast.de) API. Stellt Strompreis-Vorhersagen als REST-Endpunkt bereit – mit lokalem Caching, Fixkosten-Aufschlag, MwSt.-Berechnung, Preisobergrenze und evcc-kompatiblem Ausgabeformat.

Docker-Image: `ghcr.io/mastr/energyforecast_proxy:latest` (amd64 + arm64)

---

## Endpunkte

| Endpunkt            | Beschreibung                                              |
|---------------------|-----------------------------------------------------------|
| `GET /prices`       | Strompreisvorhersage abrufen (alle Parameter als Query)   |
| `GET /current`      | Aktuell gültigen Preis abrufen (nutzt Cache)              |
| `GET /simple/prices` | Alle Preise – Konfiguration aus Umgebungsvariablen       |
| `GET /simple/current` | Aktueller Preis – Konfiguration aus Umgebungsvariablen  |
| `GET /health`       | Dienststatus und Cache-Statistiken                        |

---

## Parameter (`/prices` und `/current`)

| Parameter           | Typ    | Standard  | Beschreibung                                                        |
|---------------------|--------|-----------|---------------------------------------------------------------------|
| `token`             | string | –         | API-Token für energyforecast.de **(Pflicht)**                       |
| `horizon`           | int    | `96`      | Zeithorizont in Stunden: `48` oder `96`                             |
| `resolution`        | string | `hourly`  | Auflösung: `hourly` oder `quarter_hourly`                           |
| `fixed_net_cost`    | float  | `0.0`     | Netzgebühren in EUR/kWh (z. B. `0.08`)                             |
| `markup_costs`      | float  | `0.0`     | Anbieter-Aufschlag in EUR/kWh (z. B. `0.01` für 1 ct/kWh)         |
| `fixed_cost_other`  | float  | `0.0`     | Sonstige Fixkosten in EUR/kWh                                       |
| `vat`               | float  | `0.19`    | Mehrwertsteuer als Faktor (`0.19`) oder Prozent (`19`)              |
| `price_cap`         | float  | –         | Preisobergrenze in EUR/kWh **brutto** (nach Steuern & Gebühren, optional) |
| `cache_ttl_minutes` | int    | `60`      | Cache-Gültigkeit in Minuten (1–1440)                                |
| `resultformat`      | string | `default` | Ausgabeformat: `default` oder `evcc`                                |
| `tz`                | string | –         | Ausgabe-Zeitzone (IANA, z. B. `Europe/Berlin`). Standard: UTC       |

**Preisformel:** `(spot + fixed_net_cost + markup_costs + fixed_cost_other) × (1 + vat)`

Der Cache speichert ausschließlich Netto-Spotpreise. Fixkosten und MwSt. werden lokal berechnet.
Zwischen **12:00 und 13:30 UTC** wird der Cache für `/prices` und `/simple/prices` immer umgangen
(frische EPEX-Daten für den nächsten Tag), `/current` trifft immer den Cache.

---

## Ausgabeformate

**`default`** – Zeitstempel in UTC, Schlüssel `prices`:
```json
{ "prices": [ { "start": "2024-01-01T12:00:00Z", "end": "2024-01-01T13:00:00Z", "value": 0.2843 } ] }
```

**`evcc`** – Zeitstempel mit Offset, Schlüssel `rates` (kompatibel mit [evcc](https://docs.evcc.io)):
```json
{ "rates": [ { "start": "2024-01-01T13:00:00+01:00", "end": "2024-01-01T14:00:00+01:00", "value": 0.2843 } ] }
```

---

## Simple-Modus (lokales Deployment)

Wenn `ENERGYFORECAST_TOKEN` gesetzt ist, sind `/simple/prices` und `/simple/current` ohne
Query-Parameter nutzbar. Ideal für Home-Automation-Setups (evcc, ioBroker, Node-RED, …).

| Umgebungsvariable              | Standard  | Beschreibung                                              |
|-------------------------------|-----------|-----------------------------------------------------------|
| `ENERGYFORECAST_TOKEN`         | –         | API-Token für energyforecast.de **(Pflicht)**             |
| `ENERGYFORECAST_HORIZON`       | `96`      | Zeithorizont: `48` oder `96` Stunden                      |
| `ENERGYFORECAST_RESOLUTION`    | `hourly`  | `hourly` oder `quarter_hourly`                            |
| `ENERGYFORECAST_FIXED_NET_COST`| `0.0`     | Netzgebühren in EUR/kWh (z. B. `0.08`)                   |
| `ENERGYFORECAST_MARKUP_COSTS`  | `0.0`     | Anbieter-Aufschlag in EUR/kWh (z. B. `0.01`)             |
| `ENERGYFORECAST_FIXED_COST_OTHER` | `0.0`  | Sonstige Fixkosten in EUR/kWh                             |
| `ENERGYFORECAST_VAT`           | `0.19`    | Mehrwertsteuer (`0.19` oder `19`)                         |
| `ENERGYFORECAST_PRICE_CAP`     | –         | Preisobergrenze in EUR/kWh **brutto** (nach Steuern & Gebühren, optional) |
| `ENERGYFORECAST_CACHE_TTL`     | `60`      | Cache-Gültigkeit in Minuten                               |
| `ENERGYFORECAST_RESULT_FORMAT` | `default` | `default` oder `evcc`                                     |
| `ENERGYFORECAST_TZ`            | –         | Ausgabe-Zeitzone, z. B. `Europe/Berlin`                   |

---

## Deployment (Docker)

```bash
cp docker-compose.sample.yml docker-compose.yml
# Umgebungsvariablen in docker-compose.yml anpassen
docker compose up -d
```

## Lokaler Start

```bash
pip install fastapi uvicorn[standard] requests tzdata
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Beispielaufruf

```
GET /prices?token=DEIN_TOKEN&horizon=96&fixed_net_cost=0.08&markup_costs=0.01&vat=0.19&resultformat=evcc
GET /simple/prices
GET /simple/current
```
