#!/usr/bin/env python3
"""
Free Statcast adapter (Baseball Savant) — stdlib only, no pybaseball needed.

pybaseball is just a wrapper around Baseball Savant's CSV search endpoint, so we
hit it directly with urllib + csv and do our own per-day caching. This gives the
hitter "shape" layer the proxy model was missing: air-pull rate, barrel rate,
hard-hit rate, fly-ball rate, plus pitcher barrel/fly-ball/HR allowed.

v2.6 additions:
  - Hardened fetch: browser-grade headers + gzip + retries (Savant started
    rejecting non-browser user agents mid-2026, which silently killed the pull).
  - Cache VALIDATION: a response is only cached if it parses as a real Statcast
    CSV (header row present). Previously a blocked/HTML response was cached as
    an empty file and poisoned every later run ("days_cached" looked fine while
    batters=0). Invalid cached files are now detected, deleted, and refetched.
  - Plate-discipline layer: in-zone swings/contact (Z-Contact%), whiffs.
  - Contact-quality layer: sweet-spot rate (LA 8-32°), EV90 (90th-pct exit
    velo), average air EV (FB/LD), xwOBA-on-contact.
  - Luck layer: window HR + barrel counts (build.py turns these into
    xHR-vs-HR "due for regression" signals for hitters AND pitchers).

Endpoint (one day at a time keeps us well under Savant's ~30k-row query cap):
  https://baseballsavant.mlb.com/statcast_search/csv?...&game_date_gt=D&game_date_lt=D

Batter/pitcher IDs in the CSV are MLBAM ids and match the MLB Stats API ids.

Cache: data/cache/statcast/YYYY-MM-DD.csv.gz (raw daily pulls; re-used across runs).
"""

import os
import csv
import io
import gzip
import math
import time
import zlib
import datetime as dt
from urllib.request import urlopen, Request

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "data", "cache", "statcast")

SAVANT = ("https://baseballsavant.mlb.com/statcast_search/csv?all=true"
          "&type=details&player_type=batter&min_pitches=0"
          "&game_date_gt={d}&game_date_lt={d}")

# v2.6 — Savant (Akamai) now 403s obviously-scripted user agents. Look like a
# normal browser download: real UA + Accept + referer, and accept gzip.
FETCH_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,text/plain,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://baseballsavant.mlb.com/statcast_search",
    "Connection": "keep-alive",
}
FETCH_RETRIES = 3          # attempts per day
FETCH_BACKOFF = 4          # seconds; doubles per retry
# Columns that must exist for a pull to be considered a real Statcast CSV.
REQUIRED_COLS = {"batter", "pitcher", "launch_speed", "events", "type"}

# Savant hit-coordinate origin (home plate ≈ (125.42, 198.27), y grows toward
# home). Spray angle = atan((hc_x-125.42)/(198.27-hc_y)); pull = the outer
# third (|angle| > 15°) on the batter's pull side — matching Savant's own
# pulled-air definition. (v2.5 fix: the old check used the pull HALF of the
# field, which made league air-pull ~27% and everything look 'elite'.)
CENTER_X = 125.42
HOME_Y = 198.27
PULL_DEG = 15.0
AIR_TYPES = {"fly_ball", "line_drive"}        # HR-relevant air contact
ALL_AIR = {"fly_ball", "line_drive", "popup"}

# v2.6 — swing/contact classification from the pitch-level `description`.
SWING_DESC = {"foul", "hit_into_play", "swinging_strike", "swinging_strike_blocked",
              "foul_tip", "foul_bunt", "missed_bunt", "bunt_foul_tip"}
WHIFF_DESC = {"swinging_strike", "swinging_strike_blocked", "missed_bunt"}
ZONE_IN = {"1", "2", "3", "4", "5", "6", "7", "8", "9"}   # Savant zones 1-9 = in-zone
SWEET_LO, SWEET_HI = 8.0, 32.0                            # Savant sweet-spot LA band

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


def _looks_valid(text):
    """True when the payload is a real Statcast CSV (header w/ expected cols).

    A header-only file is VALID (legit off-day / All-Star break). An HTML block
    page, WAF error, or empty body is NOT, and must never be cached.
    """
    if not text:
        return False
    header = text.lstrip().splitlines()[0] if text.strip() else ""
    if "<" in header[:5]:               # HTML error/block page
        return False
    cols = {c.strip().strip('"') for c in header.split(",")}
    return REQUIRED_COLS.issubset(cols)


def _decode_body(raw, headers):
    """Handle gzip/deflate transfer encoding (Savant compresses when asked)."""
    enc = (headers.get("Content-Encoding") or "").lower()
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8-sig", errors="replace")


def _read_cached(path):
    """Return (rows, valid) for a cached day; invalid caches are deleted."""
    try:
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except OSError:
        text = ""
    if _looks_valid(text):
        return list(csv.DictReader(io.StringIO(text))), True
    # v2.6 — poisoned/empty cache file (written by a pre-2.6 failed fetch)
    try:
        os.remove(path)
        print(f"  [statcast] purged invalid cache {os.path.basename(path)}")
    except OSError:
        pass
    return [], False


def _fetch_day(date_str):
    """Return list of dict rows for one date, using validated disk cache."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{date_str}.csv.gz")
    if os.path.exists(path):
        rows, valid = _read_cached(path)
        if valid:
            return rows
    url = SAVANT.format(d=date_str)
    text = None
    for attempt in range(FETCH_RETRIES):
        req = Request(url, headers=FETCH_HEADERS)
        try:
            with urlopen(req, timeout=60) as r:
                body = _decode_body(r.read(), r.headers)
            if _looks_valid(body):
                text = body
                break
            print(f"  [statcast] {date_str} attempt {attempt+1}: response not a "
                  f"Statcast CSV (blocked page?) — retrying")
        except Exception as e:
            print(f"  [statcast] {date_str} attempt {attempt+1} failed: {e}")
        time.sleep(FETCH_BACKOFF * (2 ** attempt))
    if text is None:
        print(f"  [statcast] {date_str}: giving up (NOT cached — will retry next run)")
        return []
    # Cache the RAW text so the header survives even on 0-game days.
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(text)
    return list(csv.DictReader(io.StringIO(text)))


def _is_pulled(stand, hc_x, hc_y):
    """True spray-angle pull: outer third of the field on the batter's pull side."""
    if hc_x is None or hc_y is None or stand not in ("L", "R"):
        return False
    dy = HOME_Y - hc_y
    if dy <= 0:
        return False
    spray = math.degrees(math.atan((hc_x - CENTER_X) / dy))
    # LF (RHB pull side) = negative spray; RF (LHB pull side) = positive.
    return (stand == "R" and spray < -PULL_DEG) or (stand == "L" and spray > PULL_DEG)


def _pctile(sorted_vals, q):
    """Simple percentile on a pre-sorted list (linear interpolation)."""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def load_window(end_date, days=21, recent_days=7):
    """
    Aggregate Statcast over [end_date-days+1, end_date].

    Returns (batters, pitchers, meta) where batters[mlbam_id] adds (v2.6):
      z_contact_rate, zone_swings, contact_rate, swings, sweet_spot_rate,
      ev90, air_ev, air_n, xwobacon, xw_n, window_hr, barrels
    on top of the v2.5 shape keys, and pitchers[mlbam_id] adds:
      barrels_allowed, sweet_spot_allowed_rate.
    """
    end = dt.date.fromisoformat(end_date)
    dates = [(end - dt.timedelta(days=i)).isoformat() for i in range(days)]
    recent_cut = (end - dt.timedelta(days=recent_days - 1)).isoformat()

    # raw accumulators
    B = {}   # batter agg
    P = {}   # pitcher agg
    fetched, cached, failed = 0, 0, 0
    for d in dates:
        before = os.path.exists(os.path.join(CACHE, f"{d}.csv.gz"))
        rows = _fetch_day(d)
        after = os.path.exists(os.path.join(CACHE, f"{d}.csv.gz"))
        if before and after:
            cached += 1
        elif after:
            fetched += 1
        else:
            failed += 1
        for r in rows:
            bid = r.get("batter")
            pid = r.get("pitcher")
            events = (r.get("events") or "").strip()
            desc = (r.get("description") or "").strip()
            is_pa = bool(events)
            is_bbe = r.get("type") == "X"
            ls = _f(r.get("launch_speed"))
            la = _f(r.get("launch_angle"))
            lsa = r.get("launch_speed_angle")
            xw = _f(r.get("estimated_woba_using_speedangle"))
            bb = (r.get("bb_type") or "").strip()
            stand = r.get("stand")
            hc_x = _f(r.get("hc_x"))
            hc_y = _f(r.get("hc_y"))
            zone = (r.get("zone") or "").strip()
            in_zone = zone in ZONE_IN
            is_swing = desc in SWING_DESC
            is_whiff = desc in WHIFF_DESC
            is_hr = events == "home_run"
            is_barrel = (lsa == "6")
            is_hard = (ls is not None and ls >= 95)
            is_sweet = (la is not None and SWEET_LO <= la <= SWEET_HI)
            is_air = bb in AIR_TYPES
            is_fly = bb == "fly_ball"
            is_pulled_air = is_air and _is_pulled(stand, hc_x, hc_y)
            fam = PITCH_FAM.get((r.get("pitch_type") or "").strip())
            p_throws = (r.get("p_throws") or "").strip()

            if bid:
                b = B.setdefault(bid, dict(bbe=0, pa=0, barrels=0, hard=0, air=0,
                                           pulled_air=0, hr=0, sweet=0,
                                           sw=0, whiff=0, zsw=0, zct=0,
                                           air_ev=0.0, air_n=0, xw=0.0, xw_n=0,
                                           evs=[],
                                           r_pa=0, r_barrels=0, r_hr=0,
                                           fam={}, hand={}))
                if is_pa:
                    b["pa"] += 1
                if is_swing:                      # v2.6 plate-discipline layer
                    b["sw"] += 1
                    if is_whiff:
                        b["whiff"] += 1
                    if in_zone:
                        b["zsw"] += 1
                        if not is_whiff:
                            b["zct"] += 1
                if is_bbe:
                    b["bbe"] += 1
                    if is_barrel: b["barrels"] += 1
                    if is_hard:   b["hard"] += 1
                    if is_air:    b["air"] += 1
                    if is_pulled_air: b["pulled_air"] += 1
                    if is_hr:     b["hr"] += 1
                    if is_sweet:  b["sweet"] += 1
                    if ls is not None:
                        b["evs"].append(ls)
                        if is_air:
                            b["air_ev"] += ls
                            b["air_n"] += 1
                    if xw is not None:
                        b["xw"] += xw
                        b["xw_n"] += 1
                    if fam:                       # batter damage by pitch family
                        bf = b["fam"].setdefault(fam, [0, 0, 0])  # bbe, barrels, hr
                        bf[0] += 1
                        if is_barrel: bf[1] += 1
                        if is_hr:     bf[2] += 1
                    if p_throws in ("L", "R"):    # v2.5 platoon-split shape
                        bh = b["hand"].setdefault(p_throws, [0, 0, 0, 0])  # bbe, barrels, pulled_air, hr
                        bh[0] += 1
                        if is_barrel:     bh[1] += 1
                        if is_pulled_air: bh[2] += 1
                        if is_hr:         bh[3] += 1
                if d >= recent_cut:
                    if is_pa: b["r_pa"] += 1
                    if is_barrel: b["r_barrels"] += 1
                    if is_hr: b["r_hr"] += 1
            if pid:
                p = P.setdefault(pid, dict(bbe=0, pa=0, barrels=0, hard=0, air=0,
                                           fly=0, sweet=0, hr=0, pitches=0, mix={}))
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
                    if is_fly:    p["fly"] += 1
                    if is_sweet:  p["sweet"] += 1

    def rate(n, d):
        return round(n / d, 4) if d else 0.0

    batters = {}
    for bid, b in B.items():
        if b["bbe"] < 5:        # too small to trust
            continue
        evs = sorted(b["evs"])
        batters[int(bid)] = {
            "bbe": b["bbe"], "pa": b["pa"],
            "barrel_rate": rate(b["barrels"], b["bbe"]),
            "barrel_per_pa": rate(b["barrels"], b["pa"]),
            "hard_hit_rate": rate(b["hard"], b["bbe"]),
            "fb_rate": rate(b["air"], b["bbe"]),
            "air_pull_rate": rate(b["pulled_air"], b["bbe"]),
            # v2.6 — plate discipline + contact quality
            "swings": b["sw"], "zone_swings": b["zsw"],
            "contact_rate": rate(b["sw"] - b["whiff"], b["sw"]),
            "z_contact_rate": rate(b["zct"], b["zsw"]),
            "sweet_spot_rate": rate(b["sweet"], b["bbe"]),
            "ev90": round(_pctile(evs, 0.90), 1) if evs else None,
            "air_ev": round(b["air_ev"] / b["air_n"], 1) if b["air_n"] else None,
            "air_n": b["air_n"],
            "xwobacon": rate(b["xw"], b["xw_n"]),
            "xw_n": b["xw_n"],
            # v2.6 — luck layer inputs (xHR-vs-HR computed in build.py)
            "window_hr": b["hr"], "barrels": b["barrels"],
            "recent_barrels": b["r_barrels"], "recent_pa": b["r_pa"],
            "recent_hr": b["r_hr"],
            # barrels+HR per BBE within each pitch family (consumer guards on bbe)
            "by_fam": {fam: {"bbe": v[0], "barrel": v[1], "hr": v[2]}
                       for fam, v in b["fam"].items()},
            # v2.5 — raw shape counts vs LHP/RHP (consumer blends with overall)
            "vs_hand": {h: {"bbe": v[0], "barrels": v[1], "pulled_air": v[2], "hr": v[3]}
                        for h, v in b["hand"].items()},
        }
    pitchers = {}
    for pid, p in P.items():
        if p["bbe"] < 5:
            continue
        pitchers[int(pid)] = {
            "bbe": p["bbe"], "pa": p["pa"],
            "barrel_allowed_rate": rate(p["barrels"], p["bbe"]),
            "barrels_allowed": p["barrels"],
            "hard_hit_allowed_rate": rate(p["hard"], p["bbe"]),
            # v2.5 fix: true fly-ball rate (FB only). The old value counted
            # line drives too (league ~52%), so every arm maxed the FB slot.
            "fb_allowed_rate": rate(p["fly"], p["bbe"]),
            "air_allowed_rate": rate(p["air"], p["bbe"]),
            "sweet_spot_allowed_rate": rate(p["sweet"], p["bbe"]),
            "hr_allowed": p["hr"],
            "hr_per_pa_allowed": rate(p["hr"], p["pa"]),
            "pitches": p["pitches"],
            # usage share by family (only when we have a real sample of pitches)
            "mix": ({fam: rate(n, p["pitches"]) for fam, n in p["mix"].items()}
                    if p["pitches"] >= 50 else {}),
        }
    # v2.5 — league-average rates over this window (empirical-Bayes shrinkage priors)
    tb = [sum(b[k] for b in B.values()) for k in ("bbe", "barrels", "hard", "air",
                                                  "pulled_air", "pa")]
    tot_hr_b = sum(b["hr"] for b in B.values())
    tot_zsw = sum(b["zsw"] for b in B.values())
    tot_zct = sum(b["zct"] for b in B.values())
    tot_sweet = sum(b["sweet"] for b in B.values())
    tot_air_ev = sum(b["air_ev"] for b in B.values())
    tot_air_n = sum(b["air_n"] for b in B.values())
    tot_xw = sum(b["xw"] for b in B.values())
    tot_xw_n = sum(b["xw_n"] for b in B.values())
    tot_hr = sum(p["hr"] for p in P.values())
    tot_ppa = sum(p["pa"] for p in P.values())
    tot_fly = sum(p["fly"] for p in P.values())
    tot_pbbe = sum(p["bbe"] for p in P.values())
    league = {
        "barrel_rate": rate(tb[1], tb[0]),
        "hard_hit_rate": rate(tb[2], tb[0]),
        "fb_rate": rate(tb[3], tb[0]),
        "air_pull_rate": rate(tb[4], tb[0]),
        "fly_rate": rate(tot_fly, tot_pbbe),
        "hr_per_pa": rate(tot_hr, tot_ppa),
        # v2.6 — priors for the new layers
        "z_contact": rate(tot_zct, tot_zsw),
        "sweet_spot_rate": rate(tot_sweet, tb[0]),
        "air_ev": round(tot_air_ev / tot_air_n, 1) if tot_air_n else 0.0,
        "xwobacon": rate(tot_xw, tot_xw_n),
        "hr_per_barrel": rate(tot_hr_b, tb[1]),
        "bbe_total": tb[0],
    }
    meta = {"window_days": days, "recent_days": recent_days,
            "start": dates[-1], "end": end_date,
            "days_fetched": fetched, "days_cached": cached,
            "days_failed": failed,
            "batters": len(batters), "pitchers": len(pitchers),
            "league": league}
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
              f"z_ct={f['z_contact_rate']:.3f}", f"ev90={f['ev90']}", f"bbe={f['bbe']}")
