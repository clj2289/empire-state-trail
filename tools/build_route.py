#!/usr/bin/env python3
"""
Re-anchor the app to a new route line.

Everything in this app that quotes a distance is measured against one array:
ROUTE in est-core.js, a list of [lat, lng, cumulative-mile]. Swap the line and
five things go stale at once, so they are all rewritten here in one pass rather
than by hand in five places that can drift apart:

  1. ROUTE itself                       (est-core.js)
  2. the mile of every town in TOWNS    (est-core.js)
  3. the Albany hinge the two-tone line and the mileposts split on  (est-core.js)
  4. ELEV — the elevation profile, on the same mile ruler  (est-core.js)
  5. m/o — trail mile and miles-off — on every bundled POI  (data/pois-nearby.json)

Three details matter more than they look.

Direction: the app's whole milepost convention counts up from the NYC end, and
so does the prose describing it. A GPX drawn the other way round is reversed on
import rather than left to invert every number downstream.

Mileage vs. drawing: the line is simplified to ~2,000 vertices for drawing, but
the miles attached to those vertices are measured on the full-resolution track.
The two are not the same number — summing the legs of the old simplified array
gives 546 mi against the 564 it correctly declares, because a simplified line
cuts every corner. Measure first, simplify second, and the total stays honest.

Height vs. drawing: ELEV is deliberately NOT hung off ROUTE's vertices. Douglas-
Peucker keeps the points that bend in plan, which is nothing at all to do with the
points that bend in section — a mile of dead-straight towpath climbing forty feet
is exactly the stretch it throws away. So the profile is resampled off the full
track on its own fixed mile step and indexes by mile arithmetic instead, which
also means the drawn line and the drawn profile can be simplified for their own
reasons without either one deciding for the other.

Usage:
  python3 tools/build_route.py data/Judd_EST_2026.gpx
  python3 tools/build_route.py data/Judd_EST_2026.gpx --dry-run
"""

import argparse
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "est-core.js")
POIS = os.path.join(ROOT, "data", "pois-nearby.json")

GPX_NS = "{http://www.topografix.com/GPX/1/1}"
# Mile 0. The app's start, and the anchor that decides which way a track is facing.
BATTERY = (40.70464, -74.01696)
TARGET_VERTICES = 2000     # the old array was 1,973 — keep the drawing cost where it was
POI_CORRIDOR_MI = 5.2      # the loader's own ceiling (loadBundledPois); past it a POI is dropped

FT_PER_M = 3.2808399
# The profile's own ruler. A tenth of a mile is 528 feet, about three times the spacing of
# the source track, and it is the coarsest step that still draws the Walkway's 212 ft in
# 1.28 mi as a climb rather than as a corner. It also makes the lookup pure arithmetic:
# mile / ELEV_STEP is the index, with no search and nothing to keep sorted.
ELEV_STEP_MI = 0.1
# +-1 sample: a 0.3-mile window, which is barely a filter at all. It exists to take the
# single-sample steps out of a DEM-derived track — the source quantises to about a foot,
# and undoing that costs nothing real. Anything wider starts eating the short pitches that
# are the entire reason a rider opens a profile, so this is as far as it goes.
ELEV_SMOOTH = 1
# What counts as a climb. Every rise is a rise if you measure finely enough, and summing
# them all turns surveying noise into thousands of feet of phantom ascent; the convention
# the ride trackers settled on is to ignore anything under about ten feet, and the app's
# gain figures say so on their face rather than quoting a number no other tool agrees with.
GAIN_MIN_FT = 10.0


def miles_between(a, b):
    """Great-circle miles — the same formula, and the same earth radius, as
       miBetween in est-core.js, so the two never disagree about a distance."""
    p = math.pi / 180
    h = (math.sin((b[0] - a[0]) * p / 2) ** 2
         + math.cos(a[0] * p) * math.cos(b[0] * p) * math.sin((b[1] - a[1]) * p / 2) ** 2)
    return 2 * 3958.7613 * math.asin(math.sqrt(h))


def read_gpx(path):
    """Every trkpt in the file, in file order, as (lat, lon, metres). The height rides
       along with the point rather than being read in a second pass, so orienting the
       track cannot put the profile on backwards while the line comes out the right way
       round. A point with no <ele> carries None and is bridged over later — dropping it
       would shorten the track and move every mile downstream of it."""
    root = ET.parse(path).getroot()
    pts = []
    for trkpt in root.iter(GPX_NS + "trkpt"):
        try:
            el = trkpt.find(GPX_NS + "ele")
            ele = float(el.text) if el is not None and el.text else None
            pts.append((float(trkpt.get("lat")), float(trkpt.get("lon")), ele))
        except (TypeError, ValueError):
            continue
    if len(pts) < 2:
        sys.exit(f"no usable track in {path}")
    name_el = root.find(GPX_NS + "metadata/" + GPX_NS + "name")
    return pts, (name_el.text.strip() if name_el is not None and name_el.text else "")


def orient(pts):
    """Point the track at Buffalo, whichever way it was drawn. Mile 0 is the
       Battery because every milepost, every town mile and every sentence in
       est-core.js about them already says so."""
    if miles_between(pts[-1], BATTERY) < miles_between(pts[0], BATTERY):
        return list(reversed(pts)), True
    return list(pts), False


def cumulative(pts):
    """True ridden distance to each point, measured at full resolution."""
    out = [0.0]
    for i in range(1, len(pts)):
        out.append(out[-1] + miles_between(pts[i - 1], pts[i]))
    return out


def elevation(pts, cum):
    """The profile, in feet, one sample every ELEV_STEP_MI along the measured track.

       Sampled by distance rather than by track point for two reasons. The obvious one is
       that the app can then find a height by dividing. The other is that the source track
       is not evenly spaced — it spends points on city corners and skips down the canal —
       so a per-point profile draws Manhattan wide and the Mohawk narrow, which is a
       distortion of exactly the axis the chart exists to show."""
    have = [i for i, p in enumerate(pts) if p[2] is not None]
    if not have:
        sys.exit("the track has no <ele> data — nothing to build a profile from")
    # Gaps bridged from the nearest reading either side, so an interpolation across one
    # never has to ask what None plus None is.
    filled, j = [], 0
    for i, p in enumerate(pts):
        if p[2] is not None:
            filled.append(p[2])
            continue
        while j < len(have) - 1 and have[j] < i:
            j += 1
        k = have[j] if have[j] >= i else have[-1]
        filled.append(pts[k][2])

    total = cum[-1]
    n = int(total / ELEV_STEP_MI) + 1
    out, seg = [], 0
    for k in range(n):
        m = k * ELEV_STEP_MI
        while seg < len(cum) - 2 and cum[seg + 1] < m:
            seg += 1
        span = cum[seg + 1] - cum[seg]
        f = (m - cum[seg]) / span if span > 0 else 0.0
        f = 0.0 if f < 0 else 1.0 if f > 1 else f
        out.append((filled[seg] + (filled[seg + 1] - filled[seg]) * f) * FT_PER_M)

    if ELEV_SMOOTH > 0:
        w, src = ELEV_SMOOTH, out
        out = [sum(src[max(0, i - w):min(len(src), i + w + 1)])
               / len(src[max(0, i - w):min(len(src), i + w + 1)]) for i in range(len(src))]
    return [int(round(v)) for v in out]


def gain_loss(series, floor_ft=GAIN_MIN_FT):
    """Feet up and feet down, counting only runs that add up to floor_ft before they turn.
       The reference only moves when a run qualifies, so a long climb interrupted by two
       feet of dip is still one climb rather than three."""
    if not series:
        return 0.0, 0.0
    up = down = 0.0
    ref = series[0]
    for v in series[1:]:
        d = v - ref
        if d > floor_ft:
            up += d
            ref = v
        elif d < -floor_ft:
            down -= d
            ref = v
    return up, down


def simplify(pts, tol_deg):
    """Douglas–Peucker, iterative so a 19,000-point track can't blow the stack.
       Returns the indices kept, so each one can carry its own true mile across."""
    n = len(pts)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        a, b = pts[lo], pts[hi]
        kx = math.cos(a[0] * math.pi / 180)          # degrees of longitude are shorter up here
        vy, vx = b[0] - a[0], (b[1] - a[1]) * kx
        den = vx * vx + vy * vy
        worst, wi = -1.0, -1
        for i in range(lo + 1, hi):
            py, px = pts[i][0] - a[0], (pts[i][1] - a[1]) * kx
            if den > 0:
                t = (px * vx + py * vy) / den
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
            else:
                t = 0.0
            dy, dx = py - vy * t, px - vx * t
            d = dx * dx + dy * dy
            if d > worst:
                worst, wi = d, i
        if worst > tol_deg * tol_deg and wi > 0:
            keep[wi] = True
            stack.append((lo, wi))
            stack.append((wi, hi))
    return [i for i, k in enumerate(keep) if k]


def simplify_to_count(pts, target):
    """Pick the tolerance that lands nearest the target vertex count. Bisection
       on tolerance rather than a fixed mile step: a fixed step spends the same
       number of points on a straight canal towpath as on a city street grid."""
    lo, hi = 1e-7, 0.01
    best = None
    for _ in range(40):
        mid = (lo + hi) / 2
        idx = simplify(pts, mid)
        if best is None or abs(len(idx) - target) < abs(len(best) - target):
            best = idx
        if len(idx) > target:
            lo = mid
        else:
            hi = mid
        if abs(len(idx) - target) <= target * 0.01:
            break
    return best


def project(lat, lng, route):
    """Nearest point on the line — the leg, not the vertex — and the mile there.
       Mirrors projectRoute in est-core.js so a number computed here and the same
       number computed at runtime agree."""
    kx = math.cos(lat * math.pi / 180)
    best, mile = float("inf"), route[0][2]
    for i in range(1, len(route)):
        a, b = route[i - 1], route[i]
        ay, ax = a[0] - lat, (a[1] - lng) * kx
        vy, vx = b[0] - a[0], (b[1] - a[1]) * kx
        den = vx * vx + vy * vy
        t = (-(ax * vx + ay * vy) / den) if den > 0 else 0.0
        t = 0.0 if t < 0 else 1.0 if t > 1 else t
        dy, dx = ay + vy * t, ax + vx * t
        d = dx * dx + dy * dy
        if d < best:
            best = d
            mile = a[2] + (b[2] - a[2]) * t
    return mile, math.sqrt(best) * 69


class Grid:
    """Buckets the route into ~0.1-degree cells. Projecting 9,000 POIs against a
       2,000-vertex line is 18 million leg tests done honestly; this makes it a
       few dozen per POI. Same answer, three orders of magnitude less work."""

    CELL = 0.1

    def __init__(self, route):
        self.route = route
        self.cells = {}
        for i in range(1, len(route)):
            for p in (route[i - 1], route[i]):
                key = (int(p[0] / self.CELL), int(p[1] / self.CELL))
                self.cells.setdefault(key, set()).add(i)

    def project(self, lat, lng):
        gy, gx = int(lat / self.CELL), int(lng / self.CELL)
        legs = set()
        r = 1
        while r <= 8:
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if r > 1 and max(abs(dy), abs(dx)) != r:
                        continue
                    legs |= self.cells.get((gy + dy, gx + dx), set())
            if legs and r >= 2:
                break
            r += 1
        if not legs:
            return project(lat, lng, self.route)
        kx = math.cos(lat * math.pi / 180)
        best, mile = float("inf"), self.route[0][2]
        for i in legs:
            a, b = self.route[i - 1], self.route[i]
            ay, ax = a[0] - lat, (a[1] - lng) * kx
            vy, vx = b[0] - a[0], (b[1] - a[1]) * kx
            den = vx * vx + vy * vy
            t = (-(ax * vx + ay * vy) / den) if den > 0 else 0.0
            t = 0.0 if t < 0 else 1.0 if t > 1 else t
            dy, dx = ay + vy * t, ax + vx * t
            d = dx * dx + dy * dy
            if d < best:
                best = d
                mile = a[2] + (b[2] - a[2]) * t
        return mile, math.sqrt(best) * 69


def parse_towns(src):
    """The TOWNS literal, as data. Only the mi values are rewritten, so the rest
       of each entry is matched textually and left exactly as it was."""
    m = re.search(r"^const TOWNS = (\[.*?\n\]);", src, re.S | re.M)
    if not m:
        sys.exit("could not find `const TOWNS = [...]` in est-core.js")
    body = re.sub(r"([{,])(\w+):", r'\1"\2":', m.group(1))
    return json.loads(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gpx")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--vertices", type=int, default=TARGET_VERTICES)
    args = ap.parse_args()

    gpx_path = args.gpx if os.path.isabs(args.gpx) else os.path.join(ROOT, args.gpx)
    raw, gpx_name = read_gpx(gpx_path)
    print(f"read {len(raw):,} track points from {os.path.basename(gpx_path)}"
          + (f'  ("{gpx_name}")' if gpx_name else ""))

    pts, flipped = orient(raw)
    print("direction:", "reversed to put mile 0 at the Battery" if flipped else "already runs NYC -> Buffalo")

    cum = cumulative(pts)
    total = cum[-1]
    print(f"true length: {total:.2f} mi")

    idx = simplify_to_count(pts, args.vertices)
    route = []
    last_mi = -1.0
    for i in idx:
        mi = round(cum[i], 1)
        # 1-decimal miles on a dense stretch can repeat or step back; the binary
        # searches in milePoint/routePtAt assume the column only ever climbs.
        if mi <= last_mi:
            mi = round(last_mi + 0.1, 1)
        last_mi = mi
        route.append([round(pts[i][0], 5), round(pts[i][1], 5), mi])
    route[-1][2] = round(total, 1)
    if route[-1][2] <= route[-2][2]:
        route[-1][2] = round(route[-2][2] + 0.1, 1)
    print(f"simplified to {len(route):,} vertices (old array: 1,973)")

    drawn = sum(miles_between(route[i - 1], route[i]) for i in range(1, len(route)))
    print(f"  drawn length {drawn:.1f} mi vs true {total:.1f} mi "
          f"({100 * (total - drawn) / total:.1f}% cut by simplification, carried in the mile column)")

    # ---- profile ----
    elev = elevation(pts, cum)
    up, down = gain_loss(elev)
    print(f"\nprofile: {len(elev):,} samples every {ELEV_STEP_MI} mi, "
          f"{min(elev)}–{max(elev)} ft")
    print(f"  {up:,.0f} ft up / {down:,.0f} ft down on rises of {GAIN_MIN_FT:g} ft or more "
          f"(net {elev[-1] - elev[0]:+,} ft, {elev[0]} -> {elev[-1]})")

    src = open(CORE, encoding="utf-8").read()

    # ---- towns ----
    towns = parse_towns(src)
    grid = Grid(route)
    print("\ntown miles:")
    town_mi = {}
    for t in towns:
        mile, off = grid.project(t["lat"], t["lng"])
        town_mi[t["n"]] = round(mile, 1)
        note = "   <-- now a detour" if off > 1.5 else ""
        print(f"  {t['n']:<34} {t['mi']:>6} -> {round(mile, 1):>6}   ({off:.2f} mi off){note}")

    albany = town_mi.get("Albany")
    if albany is None:
        sys.exit("no Albany in TOWNS — the hinge constant has nothing to anchor to")
    print(f"\nAlbany hinge: 201.1 -> {albany}")

    # ---- POIs ----
    pois = json.load(open(POIS, encoding="utf-8"))
    kept, dropped, moved = [], 0, 0
    for p in pois:
        try:
            lat, lng = float(p["y"]), float(p["x"])
        except (KeyError, TypeError, ValueError):
            continue
        mile, off = grid.project(lat, lng)
        if off > POI_CORRIDOR_MI:
            dropped += 1
            continue
        if abs(round(mile, 1) - float(p.get("m", 0))) > 0.15:
            moved += 1
        p["m"] = round(mile, 1)
        p["o"] = round(off, 2)
        kept.append(p)
    print(f"\nbundled POIs: {len(pois):,} in -> {len(kept):,} kept, {dropped:,} now outside "
          f"the {POI_CORRIDOR_MI} mi corridor, {moved:,} re-mile-d")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    # ---- write est-core.js ----
    lit = "[" + ",".join("[" + ",".join(
        (f"{v:.5f}".rstrip("0").rstrip(".") if j < 2 else f"{v:g}")
        for j, v in enumerate(p)) + "]" for p in route) + "]"
    src, n = re.subn(r"^const ROUTE=\[\[[\s\S]*?\]\];", "const ROUTE=" + lit + ";",
                     src, count=1, flags=re.M)
    if n != 1:
        sys.exit("could not rewrite the ROUTE literal")

    # The profile goes in beside the line it belongs to. Replaced where it already exists,
    # inserted after ROUTE where it does not, so the run that introduces it and every run
    # after it both leave the file in the same shape.
    elev_lit = ("const ELEV_STEP=" + f"{ELEV_STEP_MI:g}" + ";\nconst ELEV=["
                + ",".join(str(v) for v in elev) + "];")
    src, n = re.subn(r"^const ELEV_STEP=[\d.]+;\nconst ELEV=\[[\s\S]*?\];",
                     lambda m: elev_lit, src, count=1, flags=re.M)
    if n != 1:
        src, n = re.subn(r"^const ROUTE=\[\[[\s\S]*?\]\];\n",
                         lambda m: m.group(0) + elev_lit + "\n", src, count=1, flags=re.M)
        if n != 1:
            sys.exit("could not place the ELEV literal")

    for name, mile in town_mi.items():
        pat = r'(\{s:"(?:hv|erie)",n:"' + re.escape(name) + r'",mi:)[\d.]+'
        src, k = re.subn(pat, lambda m: m.group(1) + f"{mile:g}", src, count=1)
        if k != 1:
            print(f"  ! could not rewrite the mile for {name}", file=sys.stderr)

    # The hinge was written in twice as a bare 201.1 — once for the milepost colour
    # and once for the two-tone split. Both become one named constant so the next
    # route swap has a single number to move. Which means this has two jobs, and only
    # the first run has both: introduce the constant, and set it. Doing the introduction
    # unconditionally declared a second `const HINGE_MI` on the second run and the file
    # stopped parsing — the one failure mode a rebuild script cannot be allowed to have,
    # since the thing it breaks is the thing you would read to find out why.
    if re.search(r"^const HINGE_MI=", src, re.M):
        src, k = re.subn(r"^const HINGE_MI=[\d.]+;", f"const HINGE_MI={albany:g};",
                         src, count=1, flags=re.M)
        if k != 1:
            sys.exit("could not rewrite the hinge")
    else:
        src = src.replace(
            "const OFF_TRAIL_MI=60;",
            f"const OFF_TRAIL_MI=60;\n"
            f"/* Albany: where the Hudson Valley leg hands over to the Erie Canal, and so where\n"
            f"   the drawn line changes colour and the mileposts change with it. Derived from the\n"
            f"   route rather than typed twice — tools/build_route.py rewrites it with the line. */\n"
            f"const HINGE_MI={albany:g};", 1)
        src = src.replace("return mile<=201.1 ?", "return mile<=HINGE_MI ?", 1)
        src = src.replace("ROUTE.filter(p=>p[2]<=201.1)", "ROUTE.filter(p=>p[2]<=HINGE_MI)", 1)
        src = src.replace("ROUTE.filter(p=>p[2]>=201.1)", "ROUTE.filter(p=>p[2]>=HINGE_MI)", 1)

    open(CORE, "w", encoding="utf-8").write(src)
    json.dump(kept, open(POIS, "w", encoding="utf-8"), separators=(",", ":"))
    print(f"\nwrote {os.path.relpath(CORE, ROOT)} and {os.path.relpath(POIS, ROOT)}")


if __name__ == "__main__":
    main()
