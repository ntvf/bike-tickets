#!/usr/bin/env python3
"""
ic_bike — find PKP Intercity connections that OFFER bike places, for an agent.

How it works
------------
PKP Intercity's online booking (https://ebilet.intercity.pl, system "e-IC 2.0") has
no public API and sits behind Akamai Bot Manager, which drops curl/headless clients
(HTTP 000). It is a React SPA that talks to a JSON-RPC backend:

    POST https://api-gateway.intercity.pl/server/public/endpoint/{Pociagi|Aktualizacja}
    body: {"metoda": "<method>", "urzadzenieNr": 956, ...params}

This tool launches a REAL headed Chrome (the only client Akamai lets through),
loads the site once to obtain a valid session, then calls the RPC directly from the
page context via fetch() — no fragile DOM scraping. Methods used:
  - pobierzStacje        -> station list (kod + nazwa)
  - pobierzTypyMiejsc    -> seat-type catalog (locates the bike code, normally 24)
  - wyszukajPolaczenia   -> connection search; each train lists `typyMiejsc`
                            (the place types it offers). Bike code present => the
                            train offers bike places (this is the same signal the
                            website uses to show its bike icon at the result list).

Bike availability fidelity
--------------------------
`typyMiejsc` containing the bike code is the search-step signal that bike transport
is offered/available on that train (a train with no bike capacity, e.g. EC "Chopin",
omits it). For a hard guarantee that a bike place is still bookable right now, the
e-IC flow would additionally call `sprawdzCenyLite` (reservation step); that is left
as an enhancement — see check_bike_price() stub.

Docker / headless servers (e.g. Intel N100)
-------------------------------------------
Akamai needs headed Chrome, so run under a virtual display:
    xvfb-run -a python ic_bike.py ...
Base image suggestion: mcr.microsoft.com/playwright/python + apt install xvfb fonts-liberation.

Usage
-----
Multicity: JSON list of legs on stdin or --legs.

    python ic_bike.py --legs '[
      {"from":"Warszawa Centralna","to":"Kraków Główny","date":"2026-06-20"},
      {"from":"Kraków Główny","to":"Gdańsk Główny","date":"2026-06-22"}
    ]'

    echo '[{"from":"Poznań Główny","to":"Wrocław Główny","date":"2026-07-01"}]' \
        | python ic_bike.py --json

Station names are matched fuzzily against e-IC's abbreviated names
("Warszawa Centralna" -> "Warszawa Centr.", "Kraków Główny" -> "Kraków Gł.").
"""
import sys
import json
import argparse
import unicodedata

from playwright.sync_api import sync_playwright

BASE = "https://api-gateway.intercity.pl/server/public/endpoint"
SITE = "https://ebilet.intercity.pl/"
DEVICE = 956
BIKE_FALLBACK_CODE = 24

# JS run inside the page: a tiny RPC helper over the e-IC backend.
RPC_JS = r"""
async ({path, body}) => {
  const r = await fetch("%s" + path, {
    method: "POST",
    headers: {"content-type": "application/json",
              "accept": "application/json, text/plain, */*"},
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch (e) {}
  return {status: r.status, data};
}
""" % BASE


_PL = str.maketrans({"ł": "l", "Ł": "l", "ł": "l"})


def _norm(s):
    s = s.translate(_PL)  # ł/Ł do not ASCII-fold via NFKD
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return s.replace(".", " ").replace("-", " ").strip()


class EicClient:
    """Drives one headed-Chrome session and exposes the e-IC RPC."""

    def __init__(self, headless=False):
        self._pw = sync_playwright().start()
        # channel="chrome" = real Chrome; required to pass Akamai. headless fails.
        self._browser = self._pw.chromium.launch(headless=headless, channel="chrome")
        self._page = self._browser.new_context(
            locale="pl-PL", viewport={"width": 1366, "height": 900}).new_page()
        self._page.goto(SITE, wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_timeout(3000)  # let Akamai sensor + app settle
        self._stations = None
        self._bike_code = None

    def rpc(self, path, body):
        body = {"urzadzenieNr": DEVICE, **body}
        res = self._page.evaluate(RPC_JS, {"path": path, "body": body})
        if res["status"] != 200:
            raise RuntimeError(f"RPC {body.get('metoda')} -> HTTP {res['status']}")
        return res["data"]

    # ---- catalogs -------------------------------------------------------
    def stations(self):
        if self._stations is None:
            self._stations = self.rpc("/Aktualizacja", {"metoda": "pobierzStacje"})["stacje"]
        return self._stations

    def bike_code(self):
        if self._bike_code is None:
            self._bike_code = BIKE_FALLBACK_CODE
            try:
                tm = self.rpc("/Aktualizacja", {"metoda": "pobierzTypyMiejsc"})["typyMiejsc"]
                for t in tm:
                    if any("rower" in (o.get("opis", "") + o.get("nazwa", "")).lower()
                           for o in t.get("opisy", [])):
                        self._bike_code = t["kod"]
                        break
            except Exception:
                pass
        return self._bike_code

    def resolve_station(self, name):
        # e-IC names are abbreviated ("Warszawa Centr.", "Kraków Gł."), so match
        # token-by-token where either side may be a prefix of the other.
        want = _norm(name)
        st = self.stations()
        exact = [s for s in st if _norm(s["nazwa"]) == want]
        if exact:
            return exact[0]

        def tok_prefix(a, b):
            return len(a) >= 2 and len(b) >= 2 and (a.startswith(b) or b.startswith(a))

        q = want.split()
        cands = []
        for s in st:
            if s["kod"] == 0:  # skip "(dowolna stacja)" wildcard
                continue
            sn = _norm(s["nazwa"]).split()
            if all(any(tok_prefix(qt, stk) for stk in sn) for qt in q):
                cands.append(s)
        if cands:
            return sorted(cands, key=lambda s: len(s["nazwa"]))[0]
        raise RuntimeError(f"station not found: {name!r}")

    # ---- search ---------------------------------------------------------
    def search(self, from_kod, to_kod, date, direct=False):
        r = self.rpc("/Pociagi", {
            "metoda": "wyszukajPolaczenia",
            "dataWyjazdu": f"{date} 00:00:00",
            "dataPrzyjazdu": f"{date} 23:59:59",
            "stacjaWyjazdu": from_kod,
            "stacjaPrzyjazdu": to_kod,
            "polaczeniaBezposrednie": 1 if direct else 0,
            "polaczeniaNajszybsze": 0,
        })
        return r.get("polaczenia", [])

    def close(self):
        try:
            self._browser.close()
        finally:
            self._pw.stop()


def find_bike_connections(legs, direct=False, only_bike=True, headless=False):
    cli = EicClient(headless=headless)
    bike = cli.bike_code()
    out = []
    try:
        for leg in legs:
            f = cli.resolve_station(leg["from"])
            t = cli.resolve_station(leg["to"])
            conns = cli.search(f["kod"], t["kod"], leg["date"], direct=direct)
            leg_out = {"from": f["nazwa"], "to": t["nazwa"], "date": leg["date"],
                       "bike_code": bike, "connections": []}
            for c in conns:
                trains = []
                for tr in c.get("pociagi", []):
                    trains.append({
                        "category": tr.get("kategoriaPociagu"),
                        "number": tr.get("nrPociagu"),
                        "name": tr.get("nazwaPociagu"),
                        "departure": tr.get("dataWyjazdu"),
                        "arrival": tr.get("dataPrzyjazdu"),
                        "bike_offered": bike in (tr.get("typyMiejsc") or []),
                    })
                bike_ok = all(x["bike_offered"] for x in trains) and trains
                if only_bike and not bike_ok:
                    continue
                leg_out["connections"].append({
                    "departure": c.get("dataWyjazdu"),
                    "arrival": c.get("dataPrzyjazdu"),
                    "duration_min": c.get("czasJazdy"),
                    "changes": max(0, len(trains) - 1),
                    "presale_available": c.get("dostepneWPrzedsprzedazy"),
                    "bike_on_whole_route": bool(bike_ok),
                    "trains": trains,
                })
            out.append(leg_out)
    finally:
        cli.close()
    return out


def _print_human(results):
    for leg in results:
        print(f"\n=== {leg['from']} -> {leg['to']}  {leg['date']}  (bike code {leg['bike_code']}) ===")
        if not leg["connections"]:
            print("  no connections offering bike places on the whole route")
            continue
        for c in leg["connections"]:
            trains = ", ".join(f"{t['category']} {t['number']}"
                               + (f" {t['name']}" if t['name'] else "") for t in c["trains"])
            flag = "BIKE OK" if c["bike_on_whole_route"] else "partial"
            print(f"  [{c['departure']} -> {c['arrival']}] {c['duration_min']}min "
                  f"changes={c['changes']} {flag}: {trains}")


def main():
    ap = argparse.ArgumentParser(description="Find PKP Intercity connections offering bike places.")
    ap.add_argument("--legs", help="JSON list of {from,to,date}; else read stdin")
    ap.add_argument("--direct", action="store_true", help="direct connections only")
    ap.add_argument("--all", action="store_true", help="show all connections, not only bike ones")
    ap.add_argument("--headless", action="store_true", help="try headless (usually blocked by Akamai)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    legs = json.loads(args.legs if args.legs else sys.stdin.read())
    if isinstance(legs, dict):
        legs = [legs]

    results = find_bike_connections(legs, direct=args.direct,
                                    only_bike=not args.all, headless=args.headless)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print_human(results)


if __name__ == "__main__":
    main()
