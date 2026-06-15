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
Two signals, in increasing strength:

1. search step (default): `typyMiejsc` contains the bike code (24) => the train
   OFFERS bike transport (a train with no bike capacity, e.g. EC "Chopin", omits
   it). Same signal the website shows as its bike icon.

2. `--verify` (live): for each bike-offering connection, fire `sprawdzCenyLite`
   (the e-IC reservation-step price call) on /Sprzedaz. A sold-out / withdrawn
   connection returns empty `ceny` / non-zero `komunikatKod`; a live one returns
   seat prices. So this confirms the connection is *purchasable right now* and
   attaches the seat price. See EicClient.check_price_lite / _interpret_lite.

Hard limit (important): PKP's public API exposes NO numeric free-bike count, and
`sprawdzCenyLite`/`sprawdzCene` only ever return *seat* offers (rodzajMiejscaKod 1),
never the bike place type. The bike is a flat-fee add-on (9.10 zł) whose capacity
is checked only at `wygenerujBilet` — on the AUTHENTICATED endpoint, which actually
reserves a spot (login required). So a true "one bike spot is free" guarantee is not
obtainable without logging in and committing a reservation. `--verify` gives the
strongest no-login signal: bike offered + connection live-sellable + presale open.

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

    # ---- live verification ---------------------------------------------
    def check_price_lite(self, conn):
        """Live price/availability check for one connection (e-IC `sprawdzCenyLite`).

        This is the reservation-step "lite" call the website fires after a
        connection is picked. It re-validates the connection against live
        inventory: a sold-out / withdrawn connection comes back with empty
        `ceny`, a non-zero `komunikatKod`, or errors. Returns the raw RPC.
        """
        odcinki = [{
            "wyjazdData": p.get("dataWyjazdu"),
            "stacjaOdKod": p.get("stacjaWyjazdu"),
            "stacjaDoKod": p.get("stacjaPrzyjazdu"),
            "pociagNr": p.get("nrPociagu"),
            "kategoriaPociagu": p.get("kategoriaPociagu"),
        } for p in conn.get("pociagi", [])]
        return self.rpc("/Sprzedaz", {
            "metoda": "sprawdzCenyLite",
            "jezyk": "PL",
            "biletTyp": 1,            # SINGLE_DOMESTIC
            "ofertyZaznaczone": [],
            "polaczenia": [{"idPolaczenia": conn.get("idPolaczenia"), "odcinki": odcinki}],
            "podrozni": [{"kodZakupowyZnizki": 1010}],  # 1010 = no discount (normal fare)
            "wersja": "web_desktop",
            "url": SITE,
        })

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


def _interpret_lite(resp):
    """Turn a raw sprawdzCenyLite reply into a live-sellability verdict.

    Note on fidelity: the public price call only ever returns *seat* offers
    (rodzajMiejscaKod 1) -- it never enumerates bike places, and PKP exposes no
    numeric free-bike count without logging in and generating a ticket (the
    `wygenerujBilet` step on the authenticated endpoint, which actually reserves
    a spot). So `live_sellable` means "this connection is purchasable right now"
    (not sold out / withdrawn). A bookable seat is necessary -- though not by
    itself sufficient -- for adding a bike, which is a flat-fee add-on.
    """
    verdict = {"live_checked": True, "live_sellable": False,
               "seat_min_price_pln": None, "message": ""}
    if resp.get("bledy"):
        verdict["message"] = _first_msg(resp["bledy"])
        return verdict
    cp = resp.get("cenyPolaczen") or []
    if not cp:
        verdict["message"] = "no offer returned"
        return verdict
    entry = cp[0]
    if entry.get("bledy"):
        verdict["message"] = _first_msg(entry["bledy"])
        return verdict
    sellable = [c for c in entry.get("ceny", [])
                if c.get("komunikatKod", 0) == 0 and not c.get("blad", False)]
    if sellable:
        verdict["live_sellable"] = True
        verdict["seat_min_price_pln"] = min(c["cena"] for c in sellable) / 100
    else:
        verdict["message"] = "no sellable offer (sold out or unavailable)"
    return verdict


def _first_msg(bledy):
    try:
        for o in bledy[0].get("opisy", []):
            if o.get("jezyk") == "EN":
                return o.get("komunikat", "")
        return bledy[0].get("opisy", [{}])[0].get("komunikat", "")
    except Exception:
        return "error"


def find_bike_connections(legs, direct=False, only_bike=True, headless=False, verify=False):
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
                conn_out = {
                    "departure": c.get("dataWyjazdu"),
                    "arrival": c.get("dataPrzyjazdu"),
                    "duration_min": c.get("czasJazdy"),
                    "changes": max(0, len(trains) - 1),
                    "presale_available": c.get("dostepneWPrzedsprzedazy"),
                    "bike_on_whole_route": bool(bike_ok),
                    "trains": trains,
                }
                # Live verification: only worth doing for bike-offering
                # connections that are presale-open.
                if verify and bike_ok and c.get("dostepneWPrzedsprzedazy"):
                    try:
                        verdict = _interpret_lite(cli.check_price_lite(c))
                    except Exception as e:
                        verdict = {"live_checked": True, "live_sellable": False,
                                   "seat_min_price_pln": None, "message": str(e)}
                    conn_out["live"] = verdict
                    conn_out["bike_bookable"] = bool(bike_ok and verdict["live_sellable"])
                leg_out["connections"].append(conn_out)
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
            live = ""
            if "live" in c:
                v = c["live"]
                if v["live_sellable"]:
                    live = f" | LIVE sellable, seat from {v['seat_min_price_pln']:.2f} zl"
                else:
                    live = f" | LIVE not sellable ({v['message']})"
            print(f"  [{c['departure']} -> {c['arrival']}] {c['duration_min']}min "
                  f"changes={c['changes']} {flag}: {trains}{live}")


def main():
    ap = argparse.ArgumentParser(description="Find PKP Intercity connections offering bike places.")
    ap.add_argument("--legs", help="JSON list of {from,to,date}; else read stdin")
    ap.add_argument("--direct", action="store_true", help="direct connections only")
    ap.add_argument("--all", action="store_true", help="show all connections, not only bike ones")
    ap.add_argument("--headless", action="store_true", help="try headless (usually blocked by Akamai)")
    ap.add_argument("--verify", action="store_true",
                    help="live-check each bike connection via sprawdzCenyLite "
                         "(confirms it is sellable now; adds price). No bike free-count exists w/o login.")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    legs = json.loads(args.legs if args.legs else sys.stdin.read())
    if isinstance(legs, dict):
        legs = [legs]

    results = find_bike_connections(legs, direct=args.direct,
                                    only_bike=not args.all, headless=args.headless,
                                    verify=args.verify)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print_human(results)


if __name__ == "__main__":
    main()
