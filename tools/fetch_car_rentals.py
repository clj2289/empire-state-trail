#!/usr/bin/env python3
"""
Pull every Avis and Budget rental counter in New York State and bundle it as
rentals.json for the app to ship (no live call at run time).

Why these two brands and not "car rental" generally: they are one company (Avis
Budget Group), they publish the same location pages under two domains, and those
pages carry the one field a rider actually needs when improvising an escape — the
station code (AVNY1, A8N). A code is what turns "there's an Avis near Warren St"
into a booking, and OpenStreetMap does not have it.

Source: each brand's own location pages, discovered from its sitemap and read for
the schema.org AutoRental block they publish for search engines. That block is the
site's own copy of the record — address, phone, coordinates, opening hours — so it
is as current as what a customer would see, and it needs no API key.

Unlike the OSM corridor bundle (see fetch_nearby_pois.py) this keeps the WHOLE
state, not a 5-mile corridor. A rental counter is the thing you reach for when the
ride has stopped working — a mechanical you can't fix, weather, an injury, a train
that doesn't take bikes — and at that point the relevant question is "where can I
get a car", not "what is near the route". The trail mile and off-route distance are
still computed for every one of them, so the app can sort by nearness and draw the
far ones differently; nothing is dropped for being far.

Output: ../data/rentals.json — a compact array of
  {b, c, n, y, x, m, o, a?, p?, h?, t?, u}
where
  b = brand ("avis" | "budget")
  c = station code as the brand prints it (AVNY1, A8N, JFK, ...)
  n = location name ("Lower Manhattan Warren St")
  y,x = lat, lng
  m = trail mile of the nearest route point
  o = miles off the route
  a = street address, one line
  p = phone
  h = hours of operation, as published
  t = location type, Budget only ("Corporate", "Agency", ...)
  u = the brand's page for this location

No third-party deps — standard library only.

Usage:
  python3 tools/fetch_car_rentals.py                 # both brands, all of NY
  python3 tools/fetch_car_rentals.py --brand avis    # one brand
  python3 tools/fetch_car_rentals.py --limit 5       # a quick test
  python3 tools/fetch_car_rentals.py --state nj      # another state, same shape
"""

import argparse, gzip, io, json, math, os, re, ssl, sys, threading, time
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from html import unescape

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(ROOT, "est-core.js")
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "rentals.json")

# Both sites serve a full desktop page to a browser UA and a redirect loop to
# anything that looks automated, so we ask the way a browser asks.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Location pages are ~1 MB of framework each and there are a few hundred of them.
# Six at a time finishes in about a minute and is gentle enough that neither site
# has ever rate-limited a run; the pause is per-request inside each worker.
WORKERS = 6
PAUSE_S = 0.4
RETRIES = 3
TIMEOUT = 45

BRANDS = {
    # Avis files its US pages under a continent segment, Budget does not. The path
    # shape is the only difference that matters for discovery — everything after it
    # is /<state>/<city>/<code>.
    "avis": {"host": "www.avis.com", "prefix": "/en/locations/nam/us/",
             "sitemap": "https://www.avis.com/sitemap.xml"},
    "budget": {"host": "www.budget.com", "prefix": "/en/locations/us/",
               "sitemap": "https://www.budget.com/all-us-locations.xml"},
}


def _ssl_context(insecure):
    """A python.org build carries no trust store of its own, so a default context
    verifies against nothing and every fetch fails the cert check while curl on the
    same machine is fine. Try certifi, then the OS bundle macOS keeps at
    /etc/ssl/cert.pem, and only then the default — verification stays on in all
    three. --insecure exists for a machine where none of them are present, and
    should be a last resort: these pages are read, not trusted with anything."""
    if insecure:
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                               # noqa: BLE001 - no certifi, try the OS bundle
        pass
    for ca in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem",
               "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(ca):
            try:
                return ssl.create_default_context(cafile=ca)
            except Exception:                       # noqa: BLE001 - unreadable bundle, keep looking
                continue
    return ssl.create_default_context()


SSL_CTX = None      # set in main()


def fetch(url, tries=RETRIES):
    """GET with a browser UA, gzip handled, retried on the transient failures."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw.decode("utf-8", "replace")
        except Exception as e:                      # noqa: BLE001 - any failure is a retry
            last = e
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise last


def discover(brand, state):
    """Every location-page URL the brand's sitemap lists for one state.

    The sitemap mixes real counters in with marketing pages that live at the same
    depth ('suv-rental-new-york-city', 'driving-guide'). We do not try to tell them
    apart by their slug — a station code is only three to six characters but so is
    plenty of marketing, and the brands are not consistent. They are separated later
    by the one test that cannot be fudged: a real location page publishes
    coordinates, a marketing page does not.
    """
    cfg = BRANDS[brand]
    xml = fetch(cfg["sitemap"])
    pat = re.compile(r"https://" + re.escape(cfg["host"])
                     + re.escape(cfg["prefix"]) + state + r"/[a-z0-9-]+/[a-z0-9._-]+")
    return sorted(set(pat.findall(xml)))


def ld_blocks(html):
    """Every application/ld+json payload on the page, parsed, flattened to objects."""
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            doc = json.loads(m.group(1))
        except Exception:                           # noqa: BLE001 - a broken block is not a location
            continue
        out.extend(doc if isinstance(doc, list) else [doc])
    return out


def clean(s):
    """One line of text out of whatever the markup left behind.

    Unescaped in a loop because some headlines are encoded twice — an ampersand on
    the Upper West Side page arrives as '&amp;amp;', so one pass leaves '&amp;'
    sitting in the name. Bounded at three passes so a string of literal '&amp;'s
    can't spin here.
    """
    s = re.sub(r"<!--.*?-->", "", str(s or ""), flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    for _ in range(3):
        was = s
        s = unescape(s)
        if s == was:
            break
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip().strip(",")


def coords_from_map(url):
    """Both brands print a Google Maps link as '...?q=<lat>,<lng>'."""
    m = re.search(r"q=(-?\d+\.\d+),(-?\d+\.\d+)", str(url or ""))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def parse_address(addr):
    """schema.org PostalAddress -> one line, minus the country nobody needs to read."""
    if not isinstance(addr, dict):
        return ""
    street = clean(addr.get("streetAddress"))
    city = clean(addr.get("addressLocality"))
    region = clean(addr.get("addressRegion"))
    # Budget writes the region out in full ("New York") where Avis abbreviates it.
    # The app's own town-matching reads a two-letter state, so normalise to that.
    if len(region) > 2:
        region = US_STATE_ABBR.get(region.lower(), region)
    zipc = clean(addr.get("postalCode"))
    # "street, city, ST, zip" — four comma-separated fields, which is the shape the app's
    # poiCity() reads a town out of (it pops a trailing zip and state, and calls whatever
    # is left the city). Joining the state and zip into one "NY 11763" field, which reads
    # fine to a human, leaves that parser looking at a field starting with a state
    # abbreviation and finding no town at all — so every one of these pins would have
    # come up blank in the Town column.
    return ", ".join(x for x in (street, city, region, zipc) if x)


US_STATE_ABBR = {"new york": "NY", "new jersey": "NJ", "connecticut": "CT",
                 "pennsylvania": "PA", "massachusetts": "MA", "vermont": "VT"}

# Avis prints "(1) 518-242-4440" and Budget prints "5182424450" for counters across
# the street from each other. Both dial fine, but a rider comparing two pins should
# not have to work out that they are the same kind of number.
def norm_phone(s):
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else clean(s)


# Some of these counters are not counters a member of the public can walk up to:
# the brands publish employee-only desks inside a corporate campus, and rideshare
# depots that rent to drivers and nobody else. They are real, so they stay in the
# file, but a rider must not detour to one — the flag is what lets the app say so.
RESTRICTED = re.compile(
    r"closed to public|employees? only|empl only|uber driver|lyft driver"
    r"|not open to the public|staff only|\bonly\b.*\bdrivers\b", re.I)

# Neither brand retires a page when it retires a counter — it renames it, so the
# location's own name becomes "CLOSED January 2, 2026" or "CLOSING October 10, 2026"
# and everything else on the page stays as it was. That date is the only field that
# says whether this pin is a place you can still walk into, so it is worth parsing
# rather than leaving buried in a name.
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
CLOSE_RE = re.compile(r"clos(?:ed|ing)\s+(?:on\s+)?"
                      r"(?:(\d{4})-(\d{2})-(\d{2})|([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4}))", re.I)


def closure_date(name):
    """ISO date this counter shut or shuts, or '' when the name says nothing.

    A bare "CLOSED" with no date is still worth recording — it just cannot be dated,
    so it comes back as the sentinel '0000-00-00', which sorts before every real date
    and so reads as "already gone" to anything comparing against today.

    "Closed to Public - Uber Drivers Only" is NOT that. It is a counter that is open
    and busy and simply won't rent to you, which is what the restricted flag says —
    dating it as shut would have retired eighteen live locations on a word they
    happen to share.
    """
    m = CLOSE_RE.search(name or "")
    if m:
        if m.group(1):
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        mon = MONTHS.get(m.group(4).lower())
        if mon:
            return f"{int(m.group(6)):04d}-{mon:02d}-{int(m.group(5)):02d}"
    if RESTRICTED.search(name or ""):
        return ""
    return "0000-00-00" if re.search(r"\bclos(ed|ing)\b", name or "", re.I) else ""


# What each site puts in front of the location's own name in a headline, and the
# boilerplate it hangs off an og:title. Stripping these is what turns three different
# page templates into the one string "<name> (<code>)".
LEADIN = re.compile(r"^(car rental|rent a car (at|from|in|near)|save on car rental in|"
                    r"car rental in)\s+", re.I)
TAIL = re.compile(r"\s*(car rental)?\s*\|.*$", re.I)


def headline(html):
    """(name, code) from whichever headline this page template happens to use.

    Three templates are in play and they are not interchangeable: Avis's React city
    pages carry an id we can target, Avis's airport pages are a plain h1 that opens
    "Rent a Car at ...", and Budget's are an h2 with the name in an itemprop and the
    code loose beside it. Reading only the first left every airport in the state
    named after a paragraph of marketing copy, because the JSON-LD description is
    prose on exactly those pages. So: try each candidate in turn and take the first
    that resolves to a name followed by a parenthesised code — the code is the proof
    that we read a headline and not a sales pitch.
    """
    cands = []
    m = re.search(r'id="hero-location-headline"[^>]*>(.*?)</h1>', html, re.S)
    if m:
        cands.append(m.group(1))
    m = re.search(r'<h2>\s*<span itemprop="name">(.*?)</h2>', html, re.S)
    if m:
        cands.append(m.group(1))
    cands += re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
    if m:
        cands.append(m.group(1))

    first = ""
    for c in cands:
        t = TAIL.sub("", LEADIN.sub("", clean(c)))
        first = first or t
        mc = re.search(r"^(.*?)\s*\(([A-Z0-9]{2,8})\)\s*$", t)
        if mc:
            return mc.group(1).strip(" -–—"), mc.group(2)
    return first, ""


def parse_page(brand, url, html):
    """One location record, or None when the page is not a location page.

    The two brands publish the same facts in different places — Avis renders a React
    page with a complete JSON-LD AutoRental block, Budget renders a server-side page
    whose JSON-LD omits the coordinates but whose microdata carries them. So each
    field is taken from wherever that brand is authoritative, and the headline is
    read from the markup on both: it is the only place the station code appears
    verbatim rather than as a URL slug that may or may not be the code.
    """
    rec = {"b": brand, "u": url}

    # --- name and station code, from the page headline: "NAME (CODE)".
    rec["n"], rec["c"] = headline(html)

    # --- the schema.org record: address, phone, hours, and (Avis) coordinates.
    lat = lng = None
    for b in ld_blocks(html):
        if str(b.get("@type", "")).lower() not in ("autorental", "localbusiness"):
            continue
        geo = b.get("geo") or {}
        if isinstance(geo, dict) and geo.get("latitude") is not None:
            lat, lng = float(geo["latitude"]), float(geo["longitude"])
        if lat is None:
            lat, lng = coords_from_map(b.get("map") or b.get("hasMap"))
        rec["a"] = parse_address(b.get("address"))
        rec["p"] = norm_phone(b.get("telephone") or (b.get("address") or {}).get("telephone"))
        hrs = b.get("openingHours")
        rec["h"] = "; ".join(clean(x) for x in hrs) if isinstance(hrs, list) else clean(hrs)
        # Avis puts the location's own name in description where Budget puts prose,
        # so it is only a fallback for a headline we failed to read.
        if not rec["n"]:
            rec["n"] = clean(b.get("description"))
        break

    # --- coordinates from microdata, which is where Budget keeps them.
    if lat is None:
        mlat = re.search(r'itemprop="latitude"\s+content="(-?\d+\.\d+)"', html)
        mlng = re.search(r'itemprop="longitude"\s+content="(-?\d+\.\d+)"', html)
        if mlat and mlng:
            lat, lng = float(mlat.group(1)), float(mlng.group(1))

    # No coordinates means this was a marketing page wearing a location URL. That is
    # the filter — see discover().
    if lat is None or lng is None:
        return None
    rec["y"], rec["x"] = round(lat, 6), round(lng, 6)

    # --- location type, which only Budget publishes. Worth keeping: a Corporate
    # counter keeps its posted hours, an Agency is somebody's dealership desk that
    # closes when they feel like it.
    mt = re.search(r'loc-type["\'][^>]*></span>.*?</p>\s*<p>\s*<span>(.*?)</span>', html, re.S)
    if mt:
        rec["t"] = clean(mt.group(1))

    # A record with no code is still a place you can rent a car; the code is the
    # nice-to-have. Fall back to the URL slug, which is the code on both sites
    # whenever the slug is short and not obviously a phrase.
    if not rec["c"]:
        slug = url.rsplit("/", 1)[-1]
        if re.fullmatch(r"[a-z0-9]{2,8}", slug):
            rec["c"] = slug.upper()

    # The city segment of the URL carries this too ('bae-systems-only-endicott'),
    # and it is the only signal on a page whose headline reads as an ordinary name.
    if RESTRICTED.search(rec["n"]) or RESTRICTED.search(url.replace("-", " ")):
        rec["r"] = 1
    z = closure_date(rec["n"])
    if z:
        rec["z"] = z

    return rec


# ---- route projection: the same corridor maths the OSM bundle uses, so a mile
# printed against a rental counter means the same thing as a mile printed against
# a grocery store. See fetch_nearby_pois.py.

def load_route():
    src = open(CORE, encoding="utf-8").read()
    m = re.search(r"const ROUTE=(\[\[.*?\]\]);", src, re.S)
    if not m:
        sys.exit("Could not find `const ROUTE=[...]` in est-core.js")
    return [(float(p[0]), float(p[1]), float(p[2])) for p in json.loads(m.group(1))]


def miles_between(lat1, lon1, lat2, lon2):
    mlat = math.radians((lat1 + lat2) / 2)
    return math.hypot((lon2 - lon1) * math.cos(mlat) * 69.17, (lat2 - lat1) * 69.17)


def build_grid(route):
    grid = {}
    for i, (lat, lon, _mi) in enumerate(route):
        grid.setdefault((round(lat, 1), round(lon, 1)), []).append(i)
    return grid


def nearest_on_route(lat, lon, route, grid):
    """(off_miles, trail_mile) for the nearest route vertex.

    The cell neighbourhood that serves the 5-mile OSM sweep is not enough here:
    these are kept statewide, and a counter out on Long Island is a hundred miles
    from any route vertex, so its neighbourhood is empty and the full scan is the
    answer rather than an error case.
    """
    best_d, best_mi = 1e9, None
    for dla in (-0.1, 0, 0.1):
        for dlo in (-0.1, 0, 0.1):
            for i in grid.get((round(lat + dla, 1), round(lon + dlo, 1)), ()):
                rlat, rlon, rmi = route[i]
                d = miles_between(lat, lon, rlat, rlon)
                if d < best_d:
                    best_d, best_mi = d, rmi
    if best_mi is None:
        for rlat, rlon, rmi in route:
            d = miles_between(lat, lon, rlat, rlon)
            if d < best_d:
                best_d, best_mi = d, rmi
    return best_d, best_mi


# How close two counters have to be to count as the same site. 0.12 mi is about 200 m
# — wide enough to join a rideshare desk logged at the kerb with the branch it lives
# inside (Colonie Center's A23 and UBA23 are 0.11 mi apart on paper), and tight enough
# that two genuinely separate Midtown counters a few blocks apart stay separate.
SAME_SITE_MI = 0.12


def tag_colocated(recs):
    """Record which other counters share each one's site, as ["avis:H6M", ...].

    Avis and Budget are one company and they staff the same desk constantly, so the
    same kerb turns up as two or three records. That is not a duplicate to collapse —
    they price and stock separately, and a rider wants both — but it IS the fact that
    makes the rest of a pin readable.

    It matters most for the rideshare desks. Avis publishes "UBH6M — Closed to Public,
    Uber Drivers Only" at 1477 Main St, Buffalo, which reads as a place not to go; the
    same address is also plain Avis H6M and Budget BU2, open to anyone. The restriction
    is on that booking code, not on the building, so a pin that says "don't come here"
    is wrong about the only thing a rider would act on. Knowing what else is on the spot
    is what turns the warning back into a fact.
    """
    for r in recs:
        near = []
        for o in recs:
            if o is r:
                continue
            if miles_between(r["y"], r["x"], o["y"], o["x"]) <= SAME_SITE_MI:
                near.append(o["b"] + ":" + (o.get("c") or "?"))
        if near:
            r["k"] = sorted(near)


_lock = threading.Lock()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", choices=["avis", "budget"], help="just one brand")
    ap.add_argument("--state", default="ny", help="two-letter state slug (default ny)")
    ap.add_argument("--limit", type=int, help="only the first N pages per brand (a test)")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (only if the cert check fails locally)")
    args = ap.parse_args()

    global SSL_CTX
    SSL_CTX = _ssl_context(args.insecure)

    brands = [args.brand] if args.brand else ["avis", "budget"]
    jobs = []
    for b in brands:
        urls = discover(b, args.state)
        if args.limit:
            urls = urls[:args.limit]
        print(f"{b}: {len(urls)} candidate pages in {args.state.upper()}", file=sys.stderr)
        jobs += [(b, u) for u in urls]

    recs, skipped, failed = [], [], []

    def work(job):
        brand, url = job
        try:
            html = fetch(url)
            time.sleep(PAUSE_S)
            rec = parse_page(brand, url, html)
        except Exception as e:                      # noqa: BLE001 - report, never abort the run
            with _lock:
                failed.append((url, repr(e)))
                print(f"  !! {url}: {e}", file=sys.stderr)
            return
        with _lock:
            if rec is None:
                skipped.append(url)
            else:
                recs.append(rec)
                print(f"  {brand:6s} {rec['c'] or '?????':6s} {rec['n'][:44]:44s}"
                      f" {rec['y']:.4f},{rec['x']:.4f}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        list(ex.map(work, jobs))

    # Two brands can list the same counter twice (Avis and Budget share a few
    # downtown desks) but they are separate records with separate codes, so the only
    # duplicate worth collapsing is the same brand and code arriving twice from two
    # URL spellings.
    seen, uniq = set(), []
    for r in sorted(recs, key=lambda r: (r["b"], r.get("c", ""), r["u"])):
        key = (r["b"], r.get("c") or r["u"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    route = load_route()
    grid = build_grid(route)
    for r in uniq:
        off, mi = nearest_on_route(r["y"], r["x"], route, grid)
        r["m"] = round(mi, 2) if mi is not None else None
        r["o"] = round(off, 2)

    tag_colocated(uniq)
    uniq.sort(key=lambda r: (r["o"], r["b"]))
    out = [{k: v for k, v in r.items() if v not in ("", None)} for r in uniq]
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")

    near = sum(1 for r in uniq if r["o"] <= 5)
    print(f"\n{len(out)} locations -> {args.out}"
          f"  ({near} within 5 mi of the trail, {len(out) - near} beyond)", file=sys.stderr)
    print(f"skipped {len(skipped)} non-location pages, {len(failed)} fetch failures",
          file=sys.stderr)
    for u, e in failed:
        print(f"  FAILED {u} {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
