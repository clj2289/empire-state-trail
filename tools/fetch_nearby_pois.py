#!/usr/bin/env python3
"""
Pull every touring-relevant OpenStreetMap POI within 5 mi of the Empire State Trail
and bundle it as pois-nearby.json for the app to ship permanently (no live Overpass
call at run time).

Route source: the ROUTE array already in est-core.js (so the corridor always matches
the app's own line, and every point carries its trail mile).

Output: ../data/pois-nearby.json — a compact array of
  {c, n, y, x, m, o, id, a?, p?, u?}
where
  c  = bundled category key (nb-grocery, nb-food, ...), matching CATCFG in est-core.js
  n  = name
  y,x= lat, lng
  m  = trail mile of the nearest route point
  o  = miles off the route (<= 5)
  id = OSM element id ("node/123", "way/456")
  a,p,u = address / phone / website, only when present

By default NYC (below the Bronx/Westchester line) is skipped — see SKIP_NYC_MILE.

Usage:
  python3 tools/fetch_nearby_pois.py               # default: skip NYC, write ../data/pois-nearby.json
  python3 tools/fetch_nearby_pois.py --include-nyc  # the whole trail, Battery Park up
  python3 tools/fetch_nearby_pois.py --limit 2     # only the first 2 route chunks (a quick test)
  python3 tools/fetch_nearby_pois.py --from-mile 22 --to-mile 300  # a custom mile window

No third-party deps — standard library only.
"""

import http.client, json, math, os, re, ssl, sys, time, urllib.request, urllib.error, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(ROOT, "est-core.js")
DATA = os.path.join(ROOT, "data")
OUT  = os.path.join(DATA, "pois-nearby.json")             # the bundle the app ships (clean runs only)
PARTIAL = os.path.join(DATA, "pois-nearby.partial.json")  # a run with gaps lands here, never clobbering OUT

RADIUS_M   = 8047          # 5 miles in metres
CORRIDOR_MI = 5.0          # keep only POIs within this many trail-miles of the route
CHUNK_MI   = 55            # miles of route per Overpass query (halved on failure, see collect())
MIN_CHUNK_MI = 6           # don't subdivide a failing chunk finer than this
SAMPLE_MI  = 1.0           # spacing of the around-polyline points inside a chunk
PAUSE_S    = 3             # polite pause after each successful chunk
RETRIES    = 5             # passes over the endpoint before a chunk is subdivided
BACKOFF    = 20            # base seconds between retry passes (grows per pass, capped)
SKIP_NYC_MILE = 17.0       # default start: the Bronx/Westchester line. Manhattan sits >5 mi
                           # below it, so the five boroughs drop out. --include-nyc overrides.
# We probed every well-known public mirror against a New York query: kumi.systems times out,
# private.coffee 404s, osm.ch answers HTTP 200 with ZERO elements (it holds no North American
# data — that false-empty is exactly what silently produced the earlier gaps), and mail.ru /
# osm.jp are unreachable or have bad certs. Only the reference instance actually serves this
# region, so we rely on it alone and stay polite about its rate limit (see de_slot_wait).
ENDPOINTS  = [
    "https://overpass-api.de/api/interpreter",
]
STATUS_URL = "https://overpass-api.de/api/status"

# macOS Python ships without a CA bundle, so verified TLS often fails. Prefer certifi if
# it's installed; otherwise the system default; and if a cert check fails on this public,
# read-only API, drop to an unverified context rather than dying (or pass --insecure).
def _ssl_context(insecure):
    if insecure:
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

SSL_CTX = None      # set in main()
INSECURE = False    # flips true if a cert check fails (or --insecure is passed)

# (category key, {osm_key: value_regex}). First match wins, mirroring osmCat() in the app.
# The app's CATCFG must have a matching 'nb-*' entry for each key here.
CATS = [
    ("nb-grocery",     [("shop", r"^(supermarket|grocery|greengrocer|health_food|farm)$")]),
    ("nb-convenience", [("shop", r"^(convenience|general|kiosk)$")]),
    ("nb-food",        [("amenity", r"^(restaurant|fast_food|cafe|pub|bar|ice_cream|food_court|biergarten)$")]),
    ("nb-lodging",     [("tourism", r"^(hotel|motel|guest_house|hostel)$")]),
    ("nb-camp",        [("tourism", r"^(camp_site|caravan_site)$")]),
    ("nb-fuel",        [("amenity", r"^(fuel)$")]),
    ("nb-water",       [("amenity", r"^(drinking_water|toilets)$")]),
    ("nb-bike",        [("shop", r"^(bicycle)$")]),
    ("nb-pharmacy",    [("amenity", r"^(pharmacy)$"), ("shop", r"^(chemist)$")]),
]

# One Overpass tag clause per (key,regex) so the whole corridor comes back in one union.
TAG_CLAUSES = []
for cat, rules in CATS:
    for k, rx in rules:
        # Overpass regexes don't take ^...$ anchors the same way; strip them and rely on ~.
        TAG_CLAUSES.append((k, rx.strip("^$")))


def load_route():
    """Extract ROUTE = [[lat,lng,mile], ...] from est-core.js."""
    src = open(CORE, encoding="utf-8").read()
    m = re.search(r"const ROUTE=(\[\[.*?\]\]);", src, re.S)
    if not m:
        sys.exit("Could not find `const ROUTE=[...]` in est-core.js")
    route = json.loads(m.group(1))
    return [(float(p[0]), float(p[1]), float(p[2])) for p in route]


def miles_between(lat1, lon1, lat2, lon2):
    """Equirectangular miles — plenty accurate at this scale, and fast."""
    mlat = math.radians((lat1 + lat2) / 2)
    dx = (lon2 - lon1) * math.cos(mlat) * 69.17
    dy = (lat2 - lat1) * 69.17
    return math.hypot(dx, dy)


def build_grid(route):
    """Bucket route vertices into ~0.1-degree cells for fast nearest-point lookup."""
    grid = {}
    for i, (lat, lon, mi) in enumerate(route):
        key = (round(lat, 1), round(lon, 1))
        grid.setdefault(key, []).append(i)
    return grid


def nearest_on_route(lat, lon, route, grid):
    """Return (off_miles, trail_mile) for the nearest route vertex, scanning nearby cells."""
    best_d, best_mi = 1e9, None
    for dla in (-0.1, 0, 0.1):
        for dlo in (-0.1, 0, 0.1):
            for i in grid.get((round(lat + dla, 1), round(lon + dlo, 1)), ()):
                rlat, rlon, rmi = route[i]
                d = miles_between(lat, lon, rlat, rlon)
                if d < best_d:
                    best_d, best_mi = d, rmi
    if best_mi is None:  # fall back to a full scan if the cell neighbourhood was empty
        for rlat, rlon, rmi in route:
            d = miles_between(lat, lon, rlat, rlon)
            if d < best_d:
                best_d, best_mi = d, rmi
    return best_d, best_mi


def sample_chunk(points, step_mi):
    """Downsample a run of route points to roughly one every step_mi."""
    out = [points[0]]
    for p in points[1:]:
        if abs(p[2] - out[-1][2]) >= step_mi:
            out.append(p)
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def chunks(route, chunk_mi):
    """Split the route into runs spanning ~chunk_mi of trail."""
    runs, cur, base = [], [], route[0][2]
    for p in route:
        if cur and abs(p[2] - base) >= chunk_mi:
            runs.append(cur)
            cur, base = [cur[-1]], p[2]   # overlap one point so radii meet
        cur.append(p)
    if len(cur) > 1:
        runs.append(cur)
    return runs


def overpass_query(sample):
    """Build one Overpass QL query for a polyline of sampled route points."""
    poly = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon, _ in sample)
    lines = ["[out:json][timeout:180];", "("]
    for key, rx in TAG_CLAUSES:
        lines.append(f'  nwr(around:{RADIUS_M},{poly})["{key}"~"{rx}"];')
    lines.append(");")
    lines.append("out center tags;")
    return "\n".join(lines)


class OverpassError(Exception):
    """A chunk did not come back cleanly (transport error, HTTP error, or a
    server-side timeout/throttle disguised as a 200)."""


def _looks_like_timeout(payload, raw):
    """Overpass answers HTTP 200 with a `remark` when the query times out or gets
    rate-limited server-side. That MUST count as a failure — otherwise the chunk's
    POIs silently vanish and we ship a gap (this is exactly how TM 140-182 and
    360-402 went missing on the first run)."""
    remark = payload.get("remark", "") if isinstance(payload, dict) else ""
    blob = (remark + " " + (raw or "")).lower()
    return any(s in blob for s in ("timed out", "runtime error", "rate_limited",
                                   "too many requests", "please try again later"))


def de_slot_wait():
    """overpass-api.de allots a small number of query slots per IP and, at /api/status,
    reports when the next one frees. Ask first and wait exactly that long, so we glide
    under the 429 that produced the original gaps rather than tripping it and guessing a
    backoff. Best-effort — any hiccup just falls through to the retry logic below."""
    global SSL_CTX
    try:
        req = urllib.request.Request(STATUS_URL, headers={"User-Agent": "est-trail-bundle/1.0"})
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
            txt = r.read().decode("utf-8", "replace")
    except Exception:
        return
    m = re.search(r"(\d+)\s+slots? available now", txt)
    if m and int(m.group(1)) > 0:
        return
    secs = [int(s) for s in re.findall(r"in (\d+) seconds", txt)]
    if secs:
        w = min(min(secs) + 2, 180)
        print(f"   de busy; waiting {w}s for a query slot", file=sys.stderr)
        time.sleep(w)


def run_overpass(query):
    """POST the query, waiting for a free slot first and retrying across several passes,
    until it returns a clean result. Raises OverpassError if it can't — the caller
    (collect) then subdivides and tries the halves."""
    global SSL_CTX, INSECURE
    last = "no endpoints"
    for attempt in range(RETRIES):
        for ep in ENDPOINTS:
            if "overpass-api.de" in ep:
                de_slot_wait()
            try:
                data = urllib.parse.urlencode({"data": query}).encode()
                req = urllib.request.Request(ep, data=data,
                                             headers={"User-Agent": "est-trail-bundle/1.0"})
                with urllib.request.urlopen(req, timeout=240, context=SSL_CTX) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                payload = json.loads(raw)
                if _looks_like_timeout(payload, raw):
                    raise OverpassError("server-side timeout/throttle remark")
                return payload
            except OverpassError as e:
                last = f"{ep}: {e}"
                print(f"   {ep}: {e}", file=sys.stderr)
            except urllib.error.HTTPError as e:
                last = f"{ep}: HTTP {e.code}"
                if e.code in (429, 504):
                    ra = e.headers.get("Retry-After")
                    wait = int(ra) if (ra and ra.isdigit()) else BACKOFF * (attempt + 1)
                    wait = min(wait, 120)
                    print(f"   {ep} HTTP {e.code}; waiting {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"   {ep}: HTTP {e.code}", file=sys.stderr)
            except urllib.error.URLError as e:
                # A cert failure on this public read-only API → retry unverified, once.
                if "CERTIFICATE_VERIFY" in str(e) and not INSECURE:
                    print("   TLS verification failed; falling back to an unverified "
                          "context for Overpass.", file=sys.stderr)
                    INSECURE = True
                    SSL_CTX = ssl._create_unverified_context()
                    continue
                last = f"{ep}: {e}"
                print(f"   {ep}: {e}", file=sys.stderr)
            except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:
                # A connection dropped mid-response (RemoteDisconnected), a reset/aborted
                # socket, a truncated body (IncompleteRead), a plain read TimeoutError, or a
                # garbage payload. urllib only wraps errors from the request *send* in URLError;
                # anything raised by getresponse()/read() escapes bare, so catch it here and let
                # the retry/backoff loop try again instead of aborting the whole run.
                last = f"{ep}: {e}"
                print(f"   {ep}: {e}", file=sys.stderr)
            time.sleep(3)   # brief spacing before the next endpoint
        wait = min(BACKOFF * (attempt + 1), 90)
        print(f"   pass {attempt + 1}/{RETRIES} exhausted (last: {last}); "
              f"waiting {wait}s", file=sys.stderr)
        time.sleep(wait)
    raise OverpassError(f"all endpoints failed after {RETRIES} passes ({last})")


def classify(tags):
    for cat, rules in CATS:
        for k, rx in rules:
            v = tags.get(k)
            if v and re.match(rx, v):
                return cat
    return None


def addr_of(t):
    line = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
    return ", ".join(x for x in (line, t.get("addr:city"), t.get("addr:state"),
                                 t.get("addr:postcode")) if x)


def collect(run, found, gaps):
    """Fetch one run of route points into `found`. If Overpass can't return it cleanly
    even after all the retries, split the run in half and fetch each piece — dense
    stretches (cities) that time out server-side still get covered that way. Only a
    piece already at the minimum size is given up on, and that's shouted loudly and
    recorded in `gaps` so the final verdict knows the run was not complete."""
    span = run[-1][2] - run[0][2]
    label = f"TM {run[0][2]:.0f}-{run[-1][2]:.0f}"
    try:
        res = run_overpass(overpass_query(sample_chunk(run, SAMPLE_MI)))
        els = res.get("elements", [])
        if not els and span > MIN_CHUNK_MI:
            # Zero POIs across this many miles of populated corridor is never real data —
            # it's a throttle or a bad mirror. Force a subdivide/retry instead of banking a
            # gap. (A genuinely empty short rural stretch is only trusted at MIN_CHUNK_MI.)
            raise OverpassError("empty result for a multi-mile chunk")
    except OverpassError as e:
        if span > MIN_CHUNK_MI and len(run) > 3:
            mid = len(run) // 2
            print(f"   {label} unfetchable ({e}); splitting in half", file=sys.stderr)
            collect(run[:mid + 1], found, gaps)   # overlap one point so the radii still meet
            collect(run[mid:], found, gaps)
            return
        print(f"   !! GAP: {label} could not be fetched ({e})", file=sys.stderr)
        gaps.append((run[0][2], run[-1][2], str(e)))
        return
    for el in els:
        found[(el["type"], el["id"])] = el
    print(f"   {label}: {len(els)} raw, {len(found)} unique total")
    time.sleep(PAUSE_S)   # be kind to a shared public service


def main():
    global SSL_CTX, INSECURE
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    from_mile = SKIP_NYC_MILE     # NYC is skipped by default; use --include-nyc for the whole trail
    to_mile = float("inf")
    if "--include-nyc" in sys.argv:
        from_mile = 0.0
    if "--from-mile" in sys.argv:
        from_mile = float(sys.argv[sys.argv.index("--from-mile") + 1])
    if "--to-mile" in sys.argv:
        to_mile = float(sys.argv[sys.argv.index("--to-mile") + 1])
    INSECURE = "--insecure" in sys.argv
    SSL_CTX = _ssl_context(INSECURE)

    route = load_route()
    grid = build_grid(route)          # full route → every POI still gets its true trail mile / off-distance
    # Only query the corridor inside [from_mile, to_mile]. By default from_mile is the
    # Bronx/Westchester line (SKIP_NYC_MILE), so Manhattan — >5 mi south of it — drops out;
    # --include-nyc resets it to 0 for the whole trail.
    qroute = [p for p in route if from_mile <= p[2] <= to_mile]
    if len(qroute) < 2:
        sys.exit(f"Nothing to query in mile range {from_mile}–{to_mile}.")
    runs = chunks(qroute, CHUNK_MI)
    if limit:
        runs = runs[:limit]
    print(f"Route: {len(route)} points ({route[-1][2]:.0f} mi total); "
          f"querying TM {qroute[0][2]:.0f}–{qroute[-1][2]:.0f} in {len(runs)} chunks.")

    found = {}   # (type,id) -> element
    gaps = []    # (mile_lo, mile_hi, reason) for any chunk we ultimately could not fetch
    for ci, run in enumerate(runs, 1):
        print(f"[{ci}/{len(runs)}] chunk TM {run[0][2]:.0f}–{run[-1][2]:.0f}", flush=True)
        collect(run, found, gaps)

    out = []
    kept_by_cat = {}
    for (etype, eid), e in found.items():
        tags = e.get("tags") or {}
        cat = classify(tags)
        if not cat:
            continue
        if etype == "node":
            lat, lon = e.get("lat"), e.get("lon")
        else:
            c = e.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        off, mile = nearest_on_route(lat, lon, route, grid)
        if off > CORRIDOR_MI:
            continue
        if mile < from_mile or mile > to_mile:
            continue   # the 5-mi radius reaches past the window (e.g. north Bronx below mile 17) — drop it
        name = tags.get("name") or tags.get("brand") or ""
        if not name:
            continue   # an unnamed dot on the map helps nobody plan a stop
        rec = {"c": cat, "n": name[:80], "y": round(lat, 5), "x": round(lon, 5),
               "m": round(mile, 1), "o": round(off, 2), "id": f"{etype}/{eid}"}
        a = addr_of(tags)
        if a:
            rec["a"] = a[:90]
        ph = tags.get("phone") or tags.get("contact:phone")
        if ph:
            rec["p"] = ph[:24]
        u = tags.get("website") or tags.get("contact:website")
        if u and u.startswith("http"):
            rec["u"] = u[:160]
        out.append(rec)
        kept_by_cat[cat] = kept_by_cat.get(cat, 0) + 1

    out.sort(key=lambda r: (r["m"], r["c"]))
    os.makedirs(DATA, exist_ok=True)
    # A clean run overwrites the bundle the app ships. A run with gaps must NOT clobber a
    # good committed file, so its (partial) output goes to a side file for inspection only.
    complete = not gaps
    target = OUT if complete else PARTIAL
    with open(target, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)

    size_kb = os.path.getsize(target) / 1024
    print(f"\nWrote {len(out)} POIs to {target} ({size_kb:.0f} KB)")
    for cat, rules in CATS:
        print(f"   {cat:16s} {kept_by_cat.get(cat, 0)}")

    # ---- completeness verdict ------------------------------------------------
    # The one line that answers "did it get everything?": every queried chunk came back
    # cleanly — no transport failure, no server-side throttle, no empty multi-mile stretch —
    # so nothing was silently dropped. Any gap prints loudly and the script exits non-zero,
    # which also lets a commit hook / CI refuse a partial bundle.
    bar = "=" * 64
    print("\n" + bar)
    print(f"Coverage: TM {qroute[0][2]:.0f}–{qroute[-1][2]:.0f} across {len(runs)} chunks")
    if complete:
        print("✓ COMPLETE — every chunk returned cleanly; no gaps. Safe to commit.")
        print(bar)
    else:
        print(f"✗ INCOMPLETE — {len(gaps)} gap(s) could not be fetched:")
        for lo, hi, why in gaps:
            print(f"    !! TM {lo:.0f}–{hi:.0f}  ({why})")
        print(f"\nThe committed {os.path.relpath(OUT, ROOT)} was left untouched.")
        print(f"Partial output is in {os.path.relpath(PARTIAL, ROOT)} for inspection only.")
        print("Re-run (ideally off-peak, when Overpass isn't 504-ing) until you see "
              "'✓ COMPLETE'.")
        print(bar)
        sys.exit(1)


if __name__ == "__main__":
    main()
