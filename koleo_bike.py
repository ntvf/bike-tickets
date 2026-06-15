#!/usr/bin/env python3
"""
koleo_bike — find Polish-railway connections that have AVAILABLE bike places.

Backend: KOLEO (https://api.koleo.pl), the unofficial reverse-engineered API used
by koleo.pl / the KOLEO app. Covers PKP Intercity (IC/TLK/EIC/EIP) and regional
operators (KD, KM, POLREGIO, ...).

Why auth is required
--------------------
Connection *search* is public, but the real per-train bike-place state lives behind
authenticated endpoints (seats_availability / nested_train_place_types). They return
401/404 without a token. So you need a logged-in koleo.pl account.

Two ways to authenticate (env vars):

  - KOLEO_COOKIE  full cookie header from a logged-in koleo.pl browser session.
                  REQUIRED for Google-OAuth logins (they have no password).
                  Get it: DevTools > Network > any koleo request > Request
                  Headers > copy the "cookie:" value.
  - KOLEO_USER + KOLEO_PASS  only for password (non-OAuth) accounts.

The derived koleo_token is cached at ~/.cache/koleo_bike/token.json.

Usage
-----
Multicity: pass a JSON list of legs on stdin or via --legs.

    echo '[{"from":"warszawa-centralna","to":"krakow-glowny","date":"2026-06-20"}]' \
        | python3 koleo_bike.py

    python3 koleo_bike.py --legs '[
       {"from":"Warszawa Centralna","to":"Kraków Główny","date":"2026-06-20","after":"06:00"},
       {"from":"Kraków Główny","to":"Gdańsk Główny","date":"2026-06-22"}
    ]' --json

Station can be a koleo slug ("warszawa-centralna") or a plain name ("Warszawa Centralna");
names are resolved to slugs automatically (PL diacritics handled).

Output: per leg, the connections whose trains have purchasable bike places, with the
number of FREE bike slots when obtainable.
"""

import json
import os
import sys
import time
import argparse
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

BASE = "https://api.koleo.pl"
WEB_CLIENT_ID = "83a8978d16b584621b9f9b2f7662a441f51ac39133d4441f34f08d0c79ad5042"
HEADERS = {
    "x-koleo-version": "2",
    "x-koleo-client": "Nuxt-1",
    "User-Agent": "koleo-bike-finder/1.0 (personal use)",
    "Accept": "application/json",
}
CACHE_DIR = os.path.expanduser("~/.cache/koleo_bike")
TOKEN_PATH = os.path.join(CACHE_DIR, "token.json")

# transliteration koleo uses for station slugs
_TRANSLIT = {"ł": "l", "ń": "n", "ą": "a", "ę": "e", "ś": "s", "ć": "c",
             "ó": "o", "ź": "z", "ż": "z", " ": "-", "/": "-", "_": "-"}


class KoleoError(Exception):
    pass


def _req(method, path, token=None, params=None, body=None, cookie=None,
         return_headers=False):
    url = path if path.startswith("http") else BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = dict(HEADERS)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            payload = json.loads(raw) if raw else None
            if return_headers:
                return payload, r.headers
            return payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise KoleoError(f"{method} {url} -> HTTP {e.code}: {detail}")


# ---------------------------------------------------------------- auth
def _parse_cookie(raw, key):
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(key + "="):
            return part[len(key) + 1:]
    return None


def _token_from_cookie(cookie):
    """
    Mint a koleo_token (Bearer) from a browser session cookie string.
    If the cookie already carries _koleo_token, use it; otherwise hit
    /sessions/current which sets it. Works for Google-OAuth logins.
    """
    direct = _parse_cookie(cookie, "_koleo_token")
    if direct:
        return urllib.parse.unquote(direct)
    _, hdrs = _req("GET", "/sessions/current", cookie=cookie, return_headers=True)
    for hdr in hdrs.get_all("Set-Cookie") or []:
        tok = _parse_cookie(hdr, "_koleo_token")
        if tok:
            return urllib.parse.unquote(tok)
    raise KoleoError(
        "Could not mint _koleo_token from cookie. Make sure KOLEO_COOKIE is the "
        "full cookie header from a logged-in koleo.pl session."
    )


def login():
    """Return a valid koleo_token, using cache when possible."""
    tok = _load_cached_token()
    if tok:
        return tok

    # Preferred for Google-OAuth accounts: browser session cookie.
    cookie = os.environ.get("KOLEO_COOKIE")
    if cookie:
        token = _token_from_cookie(cookie)
        _save_token(token, time.time() + 3600 - 60)
        return token

    # Fallback: username/password OAuth grant (only for password accounts).
    user, pwd = os.environ.get("KOLEO_USER"), os.environ.get("KOLEO_PASS")
    if not user or not pwd:
        raise KoleoError(
            "Bike availability needs auth. Either set KOLEO_COOKIE (full cookie "
            "header from a logged-in koleo.pl browser session — required for "
            "Google-OAuth accounts), or KOLEO_USER + KOLEO_PASS for a password "
            "account."
        )
    res = _req("POST", "/v2/main/oauth/token", body={
        "username": user, "password": pwd,
        "grant_type": "password", "client_id": WEB_CLIENT_ID,
    })
    token = res.get("access_token") or res.get("token")
    if not token:
        raise KoleoError(f"Login failed, unexpected response: {res}")
    expires = res.get("expires_in", 3600)
    _save_token(token, time.time() + float(expires) - 60)
    return token


def _load_cached_token():
    try:
        with open(TOKEN_PATH) as f:
            d = json.load(f)
        if d.get("expires_at", 0) > time.time():
            return d["token"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _save_token(token, expires_at):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        json.dump({"token": token, "expires_at": expires_at}, f)


# ---------------------------------------------------------------- stations
def slugify(name):
    s = name.strip().lower()
    out = []
    for ch in s:
        out.append(_TRANSLIT.get(ch, ch))
    slug = "".join(out)
    # collapse repeats
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def resolve_station(name):
    """Return koleo station dict {id, name, name_slug} from a slug or plain name."""
    slug = name if "-" in name and " " not in name else slugify(name)
    try:
        return _req("GET", f"/v2/main/stations/by_slug/{slug}")
    except KoleoError:
        # fallback: scan full station list for a name match
        all_st = _req("GET", "/v2/main/stations")
        want = name.strip().lower()
        for st in all_st:
            if st.get("name", "").strip().lower() == want or st.get("name_slug") == slug:
                return st
        raise KoleoError(f"Station not found: {name!r} (tried slug {slug!r})")


# ---------------------------------------------------------------- search
def search_connections(start_slug, end_slug, date, after="00:00",
                       brand_ids=None, direct=False):
    dt = datetime.strptime(f"{date} {after}", "%Y-%m-%d %H:%M")
    params = {
        "query[date]": dt.strftime("%d-%m-%Y %H:%M:%S"),
        "query[start_station]": start_slug,
        "query[end_station]": end_slug,
        "query[only_purchasable]": "true",
        "query[only_direct]": str(direct).lower(),
    }
    if brand_ids:
        params["query[brand_ids][]"] = brand_ids
    res = _req("GET", "/v2/main/connections", params=params)
    return res.get("connections", [])


# ---------------------------------------------------------------- bike places
# Place types (seat classes) to probe per koleo brand id. Bike-dedicated seats
# live inside one of these (as special_compartment_type icon "bike"); some brands
# expose a bike-only place type instead (label contains "rower"). Source: koleo-cli
# BRAND_SEAT_TYPE_MAPPING. Unknown brands fall back to the common IC/TLK classes.
BRAND_SEAT_TYPE_MAPPING = {
    45: {30: "Klasa 2", 31: "Z rowerem"},   # KD premium
    28: {4: "Klasa 1", 5: "Klasa 2"},       # IC
    1:  {4: "Klasa 1", 5: "Klasa 2"},       # TLK
    29: {4: "Klasa 1", 5: "Klasa 2"},       # EIP
    2:  {4: "Klasa 1", 5: "Klasa 2"},       # EIC
}
DEFAULT_PLACE_TYPES = {4: "Klasa 1", 5: "Klasa 2", 30: "Klasa 2", 31: "Z rowerem"}
BIKE_ICON = "bike"


def _brand_place_types(brand_id):
    return BRAND_SEAT_TYPE_MAPPING.get(brand_id, DEFAULT_PLACE_TYPES)


def bike_places_for_train(cid, train_nr, brand_id, token):
    """Count bike places (free / total) for one train of a connection."""
    free = total = 0
    checked = False
    last_err = None
    for pt_id, label in _brand_place_types(brand_id).items():
        try:
            sa = _req("GET",
                      f"/v2/main/seats_availability/{cid}/{train_nr}/{pt_id}",
                      token=token)
        except KoleoError as e:
            last_err = str(e)
            continue
        checked = True
        bike_sct = {s["id"] for s in sa.get("special_compartment_types", [])
                    if (s.get("icon") or "").lower() == BIKE_ICON}
        dedicated = "rower" in label.lower() or "bike" in label.lower()
        for s in sa.get("seats", []):
            if dedicated or s.get("special_compartment_type_id") in bike_sct:
                total += 1
                if s.get("state") == "FREE":
                    free += 1
    entry = {"train_nr": train_nr, "brand_id": brand_id,
             "free": free, "capacity": total, "available": free > 0}
    if not checked:
        entry["error"] = last_err or "no place types responded"
    return entry


def bike_places_for_connection(conn, token):
    """Per-train bike-place report for a connection (one entry per train leg)."""
    cid = conn["id"]
    out = []
    for tr in conn.get("trains", []):
        out.append(bike_places_for_train(cid, tr.get("train_nr"),
                                         tr.get("brand_id"), token))
    return out


# ---------------------------------------------------------------- driver
def find_bike_legs(legs, direct=False, brand_ids=None, only_available=True):
    token = login()
    results = []
    for leg in legs:
        frm = resolve_station(leg["from"])
        to = resolve_station(leg["to"])
        conns = search_connections(frm["name_slug"], to["name_slug"],
                                   leg["date"], leg.get("after", "00:00"),
                                   brand_ids=brand_ids, direct=direct)
        leg_out = {
            "from": frm["name"], "to": to["name"], "date": leg["date"],
            "connections": [],
        }
        for conn in conns:
            bikes = bike_places_for_connection(conn, token)
            has_avail = any(b.get("available") for b in bikes if "error" not in b)
            if only_available and not has_avail:
                continue
            leg_out["connections"].append({
                "connection_id": conn["id"],
                "departure": conn.get("departure"),
                "arrival": conn.get("arrival"),
                "changes": conn.get("changes"),
                "travel_time": conn.get("travel_time"),
                "brand_ids": conn.get("brand_ids"),
                "trains": [t.get("train_full_name") for t in conn.get("trains", [])],
                "bike_places": bikes,
            })
        results.append(leg_out)
    return results


def _print_human(results):
    for leg in results:
        print(f"\n=== {leg['from']} -> {leg['to']}  {leg['date']} ===")
        if not leg["connections"]:
            print("  no connections with available bike places")
            continue
        for c in leg["connections"]:
            trains = ", ".join(t for t in c["trains"] if t)
            print(f"  [{c['departure']} -> {c['arrival']}] {trains} "
                  f"(changes={c['changes']})")
            for b in c["bike_places"]:
                if "error" in b:
                    print(f"      bike: {b['error']}")
                    continue
                free = b["free"] if b["free"] is not None else "?"
                mark = "AVAILABLE" if b["available"] else "full/none"
                print(f"      train {b['train_nr']}: bike {mark} "
                      f"free={free} capacity={b['capacity']} price={b['price']}")


def main():
    ap = argparse.ArgumentParser(description="Find PL-rail connections with available bike places.")
    ap.add_argument("--legs", help="JSON list of legs; if omitted, read JSON from stdin")
    ap.add_argument("--direct", action="store_true", help="direct trains only")
    ap.add_argument("--brand-ids", help="comma-separated koleo brand ids to filter")
    ap.add_argument("--all", action="store_true", help="show all connections, not only ones with bike places")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    raw = args.legs if args.legs else sys.stdin.read()
    legs = json.loads(raw)
    if isinstance(legs, dict):
        legs = [legs]
    brand_ids = [int(x) for x in args.brand_ids.split(",")] if args.brand_ids else None

    try:
        results = find_bike_legs(legs, direct=args.direct, brand_ids=brand_ids,
                                 only_available=not args.all)
    except KoleoError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print_human(results)


if __name__ == "__main__":
    main()
