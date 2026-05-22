# Energyforecast Proxy

FastAPI-Proxy für die [energyforecast.de](https://www.energyforecast.de) API. Stellt Strompreis-Vorhersagen als REST-Endpunkt bereit, mit Caching, MwSt.-Berechnung und Fixkosten-Aufschlag.

## Endpunkte

| Endpunkt   | Beschreibung                              |
|------------|-------------------------------------------|
| `GET /prices` | Strompreisvorhersage abrufen           |
| `GET /health` | Dienststatus und Cache-Statistiken     |

## Parameter (`/prices`)

| Parameter         | Typ     | Standard | Beschreibung                                       |
|-------------------|---------|----------|----------------------------------------------------|
| `token`           | string  | -        | API-Token für energyforecast.de (Pflichtfeld)      |
| `horizon`         | int     | `96`     | Zeithorizont in Stunden (`48` oder `96`)           |
| `resolution`      | string  | `hourly` | Auflösung: `hourly` oder `quarter_hourly`          |
| `fixed_cost`      | float   | `0.0`    | Fixkosten in EUR/kWh (z. B. `0.16774`)             |
| `vat`             | float   | `0.19`   | Mehrwertsteuer (`0.19` oder `19`)                  |
| `cache_ttl_minutes` | int   | `60`     | Cache-Gültigkeit in Minuten (1–1440)               |
| `resultformat`    | string  | `default`| Ausgabeformat: `default` oder `evcc`               |
| `price_cap`       | float   | -        | Preisobergrenze in EUR/kWh nach Steuern/Gebühren   |

## Ausgabeformate

**default:**
```json
{ "prices": [ { "start": "2024-01-01T00:00:00Z", "end": "2024-01-01T01:00:00Z", "value": 0.285 } ] }
```

**evcc:**
```json
{ "rates": [ { "start": "2024-01-01T00:00:00+01:00", "end": "2024-01-01T01:00:00+01:00", "value": 0.285 } ] }
```

## Deployment (Docker)

```bash
docker compose up -d
```

Der Dienst läuft hinter Traefik und ist unter `batctl-energyforecast.bitcave.cc` erreichbar.

## Lokaler Start

```bash
pip install fastapi uvicorn[standard] requests
uvicorn app:app --host 0.0.0.0 --port 8000
```
