# ic_bike — PKP Intercity bike-place finder

Finds PKP Intercity connections that **offer bike places**, for multicity trips.
Drives real headed Chrome (Akamai blocks headless), calls the e-IC 2.0 JSON-RPC
backend directly. No login.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install playwright
.venv/bin/python -m playwright install chromium
# need real Google Chrome installed too (channel="chrome")
```

## Run

Input = JSON list of legs `{from, to, date}` (date `YYYY-MM-DD`). Station names
fuzzy-matched (PL diacritics ok). Reads `--legs` or stdin.

```bash
.venv/bin/python ic_bike.py --json --legs \
 '[{"from":"Warszawa Centralna","to":"Gdańsk Główny","date":"2026-07-02"}]'
```

Flags: `--json` (machine output), `--direct` (direct only), `--all` (include
no-bike connections), `--headless` (usually blocked).

## Output

JSON list per leg with `connections[]`. A connection is bookable-with-bike when
`bike_on_whole_route == true` and `presale_available == true`.

`bike_offered` = bike places offered at search time (same signal the website
shows), not a live free count. Exact count needs the reservation step
(`sprawdzCenyLite`, not implemented). Unofficial endpoints — personal use,
throttle. Headless server: `xvfb-run -a .venv/bin/python ic_bike.py ...`
