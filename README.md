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
no-bike connections), `--verify` (live-check each bike connection, adds price),
`--headless` (usually blocked).

## Output

JSON list per leg with `connections[]`. A connection offers a bike when
`bike_on_whole_route == true` and `presale_available == true`.

With `--verify`, each such connection also gets a live `sprawdzCenyLite` check:

```jsonc
"live": { "live_sellable": true, "seat_min_price_pln": 71.0, "message": "" },
"bike_bookable": true   // bike offered AND connection live-sellable now
```

`live_sellable` = connection is purchasable right now (not sold out / withdrawn).

## Bike availability — what you can and cannot know

- `bike_offered` = bike transport **offered** on that train (search-step signal,
  same as the website's bike icon).
- `--verify` adds a **live** sellability check + seat price.
- **No numeric free-bike count exists without login.** `sprawdzCenyLite` /
  `sprawdzCene` return only *seat* offers, never the bike place type. The bike is
  a flat 9.10 zł add-on whose capacity is enforced only at `wygenerujBilet` (the
  authenticated endpoint, which actually reserves a spot). A hard "≥1 bike spot
  free" guarantee therefore requires logging in and committing a reservation.
  `--verify` is the strongest no-login signal.

Unofficial endpoints — personal use, throttle.
Headless server: `xvfb-run -a .venv/bin/python ic_bike.py ...`
