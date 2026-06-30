#!/usr/bin/env python3
"""
Free Statcast adapter (Baseball Savant) — stdlib only, no pybaseball needed.

pybaseball is just a wrapper around Baseball Savant's CSV search endpoint, so we
hit it directly with urllib + csv and do our own per-day caching. This gives the
v2.3 hitter "shape" layer the proxy model was missing: air-pull rate, barrel rate,
hard-hit rate, fly-ball rate, plus pitcher barrel/fly-ball/HR allowed.

Endpoint (one day at a time keeps us well under Savant's ~30k-row query cap):
  https://baseballsavant.mlb.com/statcast_search/csv?...&game_date_gt=D&game_date_lt=D

Batter/pitcher IDs in the CSV are MLBAM ids and match the MLB Stats API ids.

Cache: data/cache/statcast/YYYY-MM-DD.csv (raw daily pulls; re-used across runs).
"""

import os
import csv
import io
import gzip
import datetime as dt
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "data", "cache", "statcast")

SAVANT = ("https://baseballsavant.mlb.com/statcast_search/csv?all=true"
          "&type=details&player_type=batter&min_pitches=0"
          "&game_date_gt={d}&game_date_lt={d}")

# Savant coordinate of home plate / dead-center for spray-angle pull detection.
CENTER_X = 125.42
AIR_TYPES = {"fly_ball", "line_drive"}        # HR-relevant air contact
ALL_AIR = {"fly_ball", "line_drive", "popup"}

# Pitch-type family map (v2.4 pitch-mix fit). Grouping into 3 families keeps the
# per-hitter samples usable over a 21-day window (raw pitch_type is too sparse).
PITCH_FAM = {
    "FF": "FB", "FA": "FB", "SI": "FB", "FT": "FB", "FC": "FB",   # fastballs/cutter
    "SL": "BRK", "ST": "BRK", "CU": "BRK", "KC": "BRK", "CS": "BRK",
    "SV": "BRK", "SC": "BRK", "KN": "BRK", "SLV": "BRK",          # breaking
    "CH": "OFF", "FS": "OFF", "FO": "OFF", "EP": "OFF",           # offspeed
}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_day(date_str):
    """Return list of dict rows for one date, using disk cache."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{date_str}.csv.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    url = SAVANT.format(d=date_str)
    req = Request(url, headers={"User-Agent": "mlb-hr-tool/2.3"})
    try:
        with urlopen(req, timeout=60) as r:
            text = r.read().decode("utf-8-sig", errors="replace")
    except Exception as e:
        print(f"  [statcast] {date_str} fetch failed: {e}")
        return []
    rows = list(csv.DictReader(io.StringIO(text)))
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
    return rows


def _is_pulled(stand, hc_x):
    if hc_x is None or stand not in ("L", "R"):
        return False
    # Right field = high hc_x, left field = low hc_x.
    return (stand == "R" and hc_x < CENTER_X) or (stand == "L" and hc_x > CENTER_X)


def load_window(end_date, days=21, recent_days=7):
    """
    Aggregate Statcast over [end_date-days+1, end_date].

    Returns (batters, pitchers, meta) where batters[mlbam_id] = {
      bbe, pa, barrel_rate, barrel_per_pa, hard_hit_rate, fb_rate,
      air_pull_rate, recent_barrels, recent_pa, recent_hr
    } and pitchers[mlbam_id] = {
      bbe, pa, barrel_allowed_rate, hard_hit_allowed_rate, fb_allowed_rate,
      hr_allowed, hr_per_pa_allowed
    }.
    """
    end = dt.date.fromisoformat(end_date)
    dates = [(end - dt.timedelta(days=i)).isoformat() for i in range(days)]
    recent_cut = (end - dt.timedelta(days=recent_days - 1)).isoformat()

    # raw accumulators
    B = {}   # batter agg
    P = {}   # pitcher agg
    fetched, cached = 0, 0
    for d in dates:
        before = os.path.exists(os.path.join(CACHE, f"{d}.csv.gz"))
        rows = _fetch_day(d)
        if before:
            cached += 1
        elif rows:
            fetched += 1
        for r in rows:
            bid = r.get("batter")
            pid = r.get("pitcher")
            events = (r.get("events") or "").strip()
            is_pa = bool(events)
            is_bbe = r.get("type") == "X"
            ls = _f(r.get("launch_speed"))
            lsa = r.get("launch_speed_angle")
            bb = (r.get("bb_type") or "").strip()
            stand = r.get("stand")
            hc_x = _f(r.get("hc_x"))
            is_hr = events == "home_run"
            is_barrel = (lsa == "6")
            is_hard = (ls is not None and ls >= 95)
            is_air = bb in AIR_TYPES
            is_pulled_air = is_air and _is_pulled(stand, hc_x)
            fam = PITCH_FAM.get((r.get("pitch_type") or "").strip())

            if bid:
                b = B.setdefault(bid, dict(bbe=0, pa=0, barrels=0, hard=0, air=0,
                                           pulled_air=0, r_pa=0, r_barrels=0, r_hr=0,
                                           fam={}))
                if is_pa:
                    b["pa"] += 1
                if is_bbe:
                    b["bbe"] += 1
                    if is_barrel: b["barrels"] += 1
                    if is_hard:   b["hard"] += 1
                    if is_air:    b["air"] += 1
                    if is_pulled_air: b["pulled_air"] += 1
                    if fam:                       # batter damage by pitch family
                        bf = b["fam"].setdefault(fam, [0, 0, 0])  # bbe, barrels, hr
                        bf[0] += 1
                        if is_barrel: bf[1] += 1
                        if is_hr:     bf[2] += 1
                if d >= recent_cut:
                    if is_pa: b["r_pa"] += 1
                    if is_barrel: b["r_barrels"] += 1
                    if is_hr: b["r_hr"] += 1
            if pid:
                p = P.setdefault(pid, dict(bbe=0, pa=0, barrels=0, hard=0, air=0, hr=0,
                                           pitches=0, mix={}))
                if fam:                           # pitcher usage by family (every pitch)
                    p["pitches"] += 1
                    p["mix"][fam] = p["mix"].get(fam, 0) + 1
                if is_pa:
                    p["pa"] += 1
                    if is_hr: p["hr"] += 1
                if is_bbe:
                    p["bbe"] += 1
                    if is_barrel: p["barrels"] += 1
                    if is_hard:   p["hard"] += 1
                    if is_air:    p["air"] += 1

    def rate(n, d):
        return round(n / d, 4) if d else 0.0

    batters = {}
    for bid, b in B.items():
        if b["bbe"] < 5:        # too small to trust
            continue
        batters[int(bid)] = {
            "bbe": b["bbe"], "pa": b["pa"],
            "barrel_rate": rate(b["barrels"], b["bbe"]),
            "barrel_per_pa": rate(b["barrels"], b["pa"]),
            "hard_hit_rate": rate(b["hard"], b["bbe"]),
            "fb_rate": rate(b["air"], b["bbe"]),
            "air_pull_rate": rate(b["pulled_air"], b["bbe"]),
            "recent_barrels": b["r_barrels"], "recent_pa": b["r_pa"],
            "recent_hr": b["r_hr"],
            # barrels+HR per BBE within each pitch family (consumer guards on bbe)
            "by_fam": {fam: {"bbe": v[0], "barrel": v[1], "hr": v[2]}
                       for fam, v in b["fam"].items()},
        }
    pitchers = {}
    for pid, p in P.items():
        if p["bbe"] < 5:
            continue
        pitchers[int(pid)] = {
            "bbe": p["bbe"], "pa": p["pa"],
            "barrel_allowed_rate": rate(p["barrels"], p["bbe"]),
            "hard_hit_allowed_rate": rate(p["hard"], p["bbe"]),
            "fb_allowed_rate": rate(p["air"], p["bbe"]),
            "hr_allowed": p["hr"],
            "hr_per_pa_allowed": rate(p["hr"], p["pa"]),
            "pitches": p["pitches"],
            # usage share by family (only when we have a real sample of pitches)
            "mix": ({fam: rate(n, p["pitches"]) for fam, n in p["mix"].items()}
                    if p["pitches"] >= 50 else {}),
        }
    meta = {"window_days": days, "recent_days": recent_days,
            "start": dates[-1], "end": end_date,
            "days_fetched": fetched, "days_cached": cached,
            "batters": len(batters), "pitchers": len(pitchers)}
    return batters, pitchers, meta


if __name__ == "__main__":
    import sys
    end = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 21
    b, p, m = load_window(end, days)
    print(m)
    # show a few leaders by air_pull_rate
    top = sorted(b.items(), key=lambda kv: kv[1]["air_pull_rate"], reverse=True)[:8]
    for bid, f in top:
        print(bid, f"air_pull={f['air_pull_rate']:.3f}", f"barrel={f['barrel_rate']:.3f}",
              f"bbe={f['bbe']}")
