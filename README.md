# ic_bike — PKP Intercity bike-place finder

Finds PKP Intercity connections that **offer bike places**, for multicity trips.
Drives real headed Chrome (Akamai blocks headless), calls the e-IC 2.0 JSON-RPC
backend directly. No login.

## Setup (once)

```bash
python3 -m venv .venv
.venv/bin/pip install playwright
.venv/bin/python -m playwright install chromium
# need real Google Chrome installed too (channel="chrome")
```

## Run

Input = JSON list of legs `{from, to, date}` (date `YYYY-MM-DD`). Station names
fuzzy-matched (PL diacritics ok).

```bash
# human output
.venv/bin/python ic_bike.py --legs \
 '[{"from":"Warszawa Centralna","to":"Kraków Główny","date":"2026-06-20"}]'

# machine output for an agent
.venv/bin/python ic_bike.py --json --legs \
 '[{"from":"Warszawa Centralna","to":"Kraków Główny","date":"2026-06-20"},
   {"from":"Kraków Główny","to":"Gdańsk Główny","date":"2026-06-22"}]'

# stdin also works
echo '[{"from":"Poznań Główny","to":"Wrocław Główny","date":"2026-07-01"}]' \
  | .venv/bin/python ic_bike.py --json
```

## Flags

- `--json`     emit JSON (use this for agents)
- `--direct`   direct connections only
- `--all`      include connections without bike places (default: bike-only)
- `--headless` try headless (usually blocked by Akamai)

## JSON output shape

```json
[
  {
    "from": "Warszawa Centr.", "to": "Kraków Gł.", "date": "2026-06-20",
    "bike_code": 24,
    "connections": [
      {
        "departure": "2026-06-20 06:08:00",
        "arrival":   "2026-06-20 10:05:00",
        "duration_min": 237,
        "changes": 0,
        "presale_available": true,
        "bike_on_whole_route": true,
        "trains": [
          {"category":"IC","number":1320,"name":"Karłowicz",
           "departure":"...","arrival":"...","bike_offered":true}
        ]
      }
    ]
  }
]
```

Agent rule: a connection is bookable-with-bike when
`bike_on_whole_route == true` (every train offers bike code 24) and
`presale_available == true`.

## Exit / errors

- Exit 0 on success. Station not found / RPC error -> non-zero + stderr traceback.
- Empty `connections` (default mode) = no bike-offering connections that day.

## Notes

- `bike_offered` = bike places **offered** on that train at search time (same signal
  the website shows). Not a hard live seat count; exact free count needs the
  reservation-step call (`sprawdzCenyLite`, not implemented).
- Unofficial endpoints. Personal / low-volume use; throttle.
- Docker / headless server (e.g. N100): `xvfb-run -a .venv/bin/python ic_bike.py ...`
```
