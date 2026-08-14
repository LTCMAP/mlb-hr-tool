#!/usr/bin/env python3
"""
Free Statcast adapter (Baseball Savant) — stdlib only, no pybaseball needed.

pybaseball is just a wrapper around Baseball Savant's CSV search endpoint, so we
hit it directly with urllib + csv and do our own per-day caching. This gives the
hitter "shape" layer the proxy model was missing: air-pull rate, barrel rate,
hard-hit rate, fly-ball rate, plus pitcher barrel/fly-ball/HR allowed.

v2.8 additions (the "Savant went dark" fix):
  - The bare 5-parameter Savant CSV query is no longer reliably accepted. We now
    send the FULL canonical statcast_search parameter set (the one Savant's own
    UI and pybaseball send, incl. hfGT game-type and hfSea season) and fall back
    through progressively simpler URL variants.
  - REAL SECOND SOURCE: if Savant is unreachable/blocked for a day, the same
    pitch-level rows are rebuilt from the MLB Stats API play-by-play feed
    (statsapi.mlb.com), which carries the identical Statcast measurements
    (launchSpeed, launchAngle, trajectory, hit coordinates, pitch type, zone).
    statsapi is the feed build.py already uses for schedule/lineups, so if the
    slate builds at all, the shape layer can now be built too. Previously a
    Savant outage meant PROXY mode: capped, shape-blind scores.
    Barrel (launch_speed_angle==6) is recomputed from EV/LA in fallback mode;
    xwOBAcon is unavailable there and regresses to the league prior.
  - No more dead time: the retry backoff no longer sleeps after the final
    attempt (21 failed days used to burn ~10 minutes doing nothing).
  - meta reports per-source day counts so build.py can label the board
    "Savant", "MLB API fallback", or "mixed" instead of silently degrading.
  - `python3 statcast.py --diag` prints a one-screen source diagnosis.

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
import json
import gzip
import math
import time
import zlib
import datetime as dt
from urllib.request import urlopen, Request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "data", "cache", "statcast")

# v2.8.1 — Savant URL variants, tried in order.
#
# HARD-WON LESSON: `group_by=name` overrides `type=details`. With it, Savant
# happily returns HTTP 200 and ~3 MB of PLAYER-AGGREGATE csv
# ("pitches","player_id","player_name","total_pitches",...) instead of the
# pitch-level rows we need. It looks like a successful fetch and is useless.
# Never put group_by in a details query. The bare query below is the one that
# actually returns pitch-level data (~17 MB/day), so it goes FIRST; the longer
# forms are de-grouped backups in case the bare form is ever rejected.
SAVANT_MIN = ("https://baseballsavant.mlb.com/statcast_search/csv?all=true"
              "&type=details&player_type=batter&min_pitches=0"
              "&game_date_gt={d}&game_date_lt={d}")
SAVANT_FULL = (
    "https://baseballsavant.mlb.com/statcast_search/csv?all=true"
    "&hfPT=&hfAB=&hfGT=R%7CPO%7CS%7C&hfPR=&hfZ=&hfStadium=&hfBBL=&hfNewZones="
    "&hfSea={season}%7C&hfSit=&player_type=batter&hfOuts=&hfOpponent="
    "&pitcher_throws=&batter_stands=&hfSA=&game_date_gt={d}&game_date_lt={d}"
    "&hfMo=&hfTeam=&home_road=&hfRO=&position=&hfInfield=&hfOutfield=&hfInn="
    "&hfBBT=&hfFlag=&metric_1=&min_pitches=0&min_results=0&min_pas=0"
    "&sort_order=desc&type=details&"
)
SAVANT_PYBB = (
    "https://baseballsavant.mlb.com/statcast_search/csv?all=true"
    "&hfPT=&hfAB=&hfBBT=&hfPR=&hfZ=&stadium=&hfBBL=&hfNewZones="
    "&hfGT=R%7CPO%7CS%7C&hfSea=&hfSit=&player_type=pitcher&hfOuts="
    "&opponent=&pitcher_throws=&batter_stands=&hfSA=&game_date_gt={d}"
    "&game_date_lt={d}&team=&position=&hfRO=&home_road=&hfFlag=&metric_1="
    "&hfInn=&min_pitches=0&min_results=0&min_abs=0&type=details&"
)
SAVANT_VARIANTS = (SAVANT_MIN, SAVANT_FULL, SAVANT_PYBB)
SAVANT_LABELS = ("bare details query", "full params (de-grouped)",
                 "pybaseball params (de-grouped)")
SAVANT = SAVANT_MIN            # kept for backwards compatibility / callers
# A details CSV must carry pitch-level columns. The aggregate CSV that
# group_by returns has player_id/total_pitches instead — reject it explicitly
# so a 200-with-wrong-shape can never be mistaken for data.
AGGREGATE_COLS = {"total_pitches", "pitch_percent", "player_name"}

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
FETCH_RETRIES = 2          # attempts per URL variant per day (3 variants)
FETCH_BACKOFF = 3          # seconds; doubles per retry
FETCH_TIMEOUT = 120        # v2.8 — a full day of type=details is slow to build
DIAG_TIMEOUT = 45          # --diag answers "up or down?"; it needn't wait 2 min
PREFETCH_WORKERS = 5       # concurrent day downloads on a cold cache
# Columns that must exist for a pull to be considered a real Statcast CSV.
REQUIRED_COLS = {"batter", "pitcher", "launch_speed", "events", "type"}

# --------------------------------------------------------------------------- #
# v2.8 — MLB Stats API fallback source
# --------------------------------------------------------------------------- #
# statsapi carries the same Statcast measurements Savant does (they come from
# the same tracking system); it is a plain public JSON API with no WAF in front
# of it, and build.py already depends on it for schedule/probables/lineups.
STATS_API = "https://statsapi.mlb.com/api/v1"
MLBAPI_HEADERS = {"User-Agent": "mlb-hr-tool/2.8 (statcast-fallback)",
                  "Accept": "application/json"}
MLBAPI_WORKERS = 6
MLBAPI_TIMEOUT = 45
# Game types that count as "real" baseball for shape purposes: regular season
# plus every postseason round. Spring (S), All-Star (A) and exhibition (E) out.
GAME_TYPES_OK = {"R", "F", "D", "L", "W", "P"}
# Savant writes a status column we don't have; we tag fallback rows so meta can
# tell the operator which source a cached day came from.
SRC_COL = "_src"
# statsapi pitch-result code -> Savant `description` vocabulary. Using the code
# (not the human string) keeps this stable across MLB copy changes.
CODE_DESC = {
    "B": "ball", "*B": "blocked_ball", "V": "ball", "I": "ball",
    "C": "called_strike", "S": "swinging_strike",
    "W": "swinging_strike_blocked", "Q": "swinging_strike_blocked",
    "F": "foul", "R": "foul", "T": "foul_tip", "O": "foul_tip",
    "L": "foul_bunt", "M": "missed_bunt", "N": "bunt_foul_tip",
    "X": "hit_into_play", "D": "hit_into_play", "E": "hit_into_play",
    "H": "hit_by_pitch", "P": "pitchout", "Y": "pitchout",
}
# Columns the fallback writes so the cached file is a drop-in for a Savant pull.
FALLBACK_COLS = ["batter", "pitcher", "events", "description", "type",
                 "launch_speed", "launch_angle", "launch_speed_angle",
                 "estimated_woba_using_speedangle", "bb_type", "stand",
                 "hc_x", "hc_y", "zone", "pitch_type", "p_throws", SRC_COL]

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
    # v2.8.1 — a group_by aggregate response is a 200 with real CSV that is the
    # WRONG SHAPE. Reject it loudly rather than caching a useless day.
    if cols & AGGREGATE_COLS:
        return False
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


def _is_barrel(ev, la):
    """Barrel classification from EV/LA (Savant's launch_speed_angle == 6).

    Only used by the MLB-API fallback, which does not ship Savant's own
    classification. Barrels start at 98 mph / 26-30°, and the LA window widens
    about a degree per side per extra mph, topping out near 8-50° at 116+.
    """
    if ev is None or la is None or ev < 98.0:
        return False
    over = ev - 98.0
    lo = max(8.0, 26.0 - over)                 # anchors: 98 -> 26, 116 -> 8
    hi = min(50.0, 30.0 + over * (20.0 / 18.0))  # anchors: 98 -> 30, 116 -> 50
    return lo <= la <= hi


def _get_json(url, timeout=MLBAPI_TIMEOUT):
    req = Request(url, headers=MLBAPI_HEADERS)
    with urlopen(req, timeout=timeout) as r:
        return json.loads(_decode_body(r.read(), r.headers))


def _game_pks(date_str, verbose=False):
    """Completed MLB game ids for a date (regular season + postseason).

    v2.8.1 — 'completed' is checked against BOTH status fields. statsapi is
    inconsistent about which one it populates, and keying on codedGameState
    alone silently returned zero games (making the fallback look broken when
    it wasn't).
    """
    url = f"{STATS_API}/schedule?sportId=1&date={date_str}"
    pks, seen, skipped = [], 0, {}
    for d in _get_json(url).get("dates", []):
        for g in d.get("games", []):
            seen += 1
            gt = g.get("gameType")
            if gt not in GAME_TYPES_OK:
                skipped[f"gameType={gt}"] = skipped.get(f"gameType={gt}", 0) + 1
                continue
            st = g.get("status", {}) or {}
            done = (st.get("codedGameState") in ("F", "O")
                    or st.get("statusCode") in ("F", "O")
                    or st.get("abstractGameState") == "Final"
                    or (st.get("detailedState") or "").startswith("Final")
                    or (st.get("detailedState") or "") == "Completed Early")
            if not done:
                key = f"state={st.get('detailedState') or st.get('abstractGameState')}"
                skipped[key] = skipped.get(key, 0) + 1
                continue
            pks.append(g["gamePk"])
    if verbose:
        print(f"  [statcast] {date_str}: {seen} scheduled, {len(pks)} usable"
              + (f", skipped {skipped}" if skipped else ""))
    return pks


def _rows_from_game(pk):
    """Pitch-level rows for one game, shaped like Savant's type=details CSV."""
    try:
        pbp = _get_json(f"{STATS_API}/game/{pk}/playByPlay")
    except Exception as e:
        print(f"  [statcast] mlbapi game {pk} failed: {e}")
        return []
    out = []
    for play in pbp.get("allPlays", []):
        m = play.get("matchup", {}) or {}
        bid = (m.get("batter") or {}).get("id")
        pid = (m.get("pitcher") or {}).get("id")
        if not bid or not pid:
            continue
        stand = (m.get("batSide") or {}).get("code") or ""
        throws = (m.get("pitchHand") or {}).get("code") or ""
        event = (play.get("result") or {}).get("eventType") or ""
        pitches = [e for e in play.get("playEvents", []) if e.get("isPitch")]
        for i, e in enumerate(pitches):
            det = e.get("details", {}) or {}
            pd_ = e.get("pitchData", {}) or {}
            hd = e.get("hitData", {}) or {}
            in_play = bool(det.get("isInPlay"))
            code = det.get("code") or ""
            desc = CODE_DESC.get(code)
            if desc is None:
                desc = ("hit_into_play" if in_play else
                        (det.get("description") or "").strip().lower().replace(" ", "_"))
            ev = _f(hd.get("launchSpeed"))
            la = _f(hd.get("launchAngle"))
            coord = hd.get("coordinates", {}) or {}
            out.append({
                "batter": str(bid), "pitcher": str(pid),
                # Savant only stamps the PA outcome on the final pitch.
                "events": event if i == len(pitches) - 1 else "",
                "description": desc,
                "type": "X" if in_play else ("B" if det.get("isBall") else "S"),
                "launch_speed": "" if ev is None else ev,
                "launch_angle": "" if la is None else la,
                "launch_speed_angle": "6" if _is_barrel(ev, la) else "",
                # not published by statsapi — regresses to the league prior
                "estimated_woba_using_speedangle": "",
                "bb_type": (hd.get("trajectory") or ""),
                "stand": stand,
                "hc_x": coord.get("coordX", ""),
                "hc_y": coord.get("coordY", ""),
                "zone": str(pd_.get("zone") or ""),
                "pitch_type": ((det.get("type") or {}).get("code") or ""),
                "p_throws": throws,
                SRC_COL: "mlbapi",
            })
    return out


def _fetch_day_mlbapi(date_str):
    """v2.8 fallback: rebuild a day of Statcast rows from statsapi play-by-play."""
    try:
        pks = _game_pks(date_str)
    except Exception as e:
        print(f"  [statcast] {date_str}: mlbapi schedule failed: {e}")
        return None
    if not pks:
        # A legitimate off-day. Return an empty (but valid) day so we cache a
        # header and never re-hammer the API for it.
        return []
    rows = []
    with ThreadPoolExecutor(max_workers=MLBAPI_WORKERS) as ex:
        for got in ex.map(_rows_from_game, pks):
            rows.extend(got)
    if not rows:
        return None                      # games existed but nothing came back
    print(f"  [statcast] {date_str}: MLB API fallback OK "
          f"({len(pks)} games, {len(rows)} pitches)")
    return rows


def _rows_to_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FALLBACK_COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


# v2.8 — circuit breaker. With Savant fully down, retrying 3 variants x 2
# attempts on all 21 days cost ~7 minutes of pure backoff before the fallback
# even started. After this many consecutive dead days we stop asking.
SAVANT_CIRCUIT_BREAK = 2
_savant_strikes = 0


def _fetch_savant(date_str):
    """Try each Savant URL variant; return CSV text or None."""
    global _savant_strikes
    if _savant_strikes >= SAVANT_CIRCUIT_BREAK:
        return None                    # tripped earlier this run; don't re-probe
    season = date_str[:4]
    for vi, tpl in enumerate(SAVANT_VARIANTS, 1):
        url = tpl.format(d=date_str, season=season)
        for attempt in range(FETCH_RETRIES):
            req = Request(url, headers=FETCH_HEADERS)
            try:
                with urlopen(req, timeout=FETCH_TIMEOUT) as r:
                    body = _decode_body(r.read(), r.headers)
                if _looks_valid(body):
                    _savant_strikes = 0        # it's alive; reset the breaker
                    return body
                print(f"  [statcast] {date_str} savant v{vi} attempt {attempt+1}: "
                      f"response is not a Statcast CSV (blocked page?)")
            except Exception as e:
                print(f"  [statcast] {date_str} savant v{vi} attempt {attempt+1} "
                      f"failed: {e}")
            # v2.8 — don't sleep after the final attempt of the final variant
            if not (vi == len(SAVANT_VARIANTS) and attempt == FETCH_RETRIES - 1):
                time.sleep(FETCH_BACKOFF * (2 ** attempt))
    _savant_strikes += 1
    if _savant_strikes == SAVANT_CIRCUIT_BREAK:
        print(f"  [statcast] Savant failed {_savant_strikes} days running — "
              f"skipping it for the rest of this run, using the MLB Stats API")
    return None


def _cache_header_ok(path):
    """Cheap validity probe: read only the header line, not the whole 17 MB."""
    try:
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as f:
            return _looks_valid(f.readline())
    except OSError:
        return False


def _ensure_day_cached(date_str):
    """Download one day to the cache if it isn't validly cached already.

    Returns "cache" | "savant" | "mlbapi" | None. Does NOT parse the CSV, so
    this is safe to run concurrently across days without holding 21 days of
    parsed rows (hundreds of MB) in memory at once.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{date_str}.csv.gz")
    if os.path.exists(path):
        if _cache_header_ok(path):
            return "cache"
        try:                            # poisoned/aggregate/empty — drop it
            os.remove(path)
            print(f"  [statcast] purged invalid cache {os.path.basename(path)}")
        except OSError:
            pass

    src = "savant"
    text = _fetch_savant(date_str)
    if text is None:
        print(f"  [statcast] {date_str}: Savant unavailable — trying MLB Stats API")
        rows = _fetch_day_mlbapi(date_str)
        if rows is None:
            print(f"  [statcast] {date_str}: giving up on both sources "
                  f"(NOT cached — will retry next run)")
            return None
        text, src = _rows_to_csv(rows), "mlbapi"

    # Write via a temp file so an interrupted run can't leave a half day behind.
    tmp = f"{path}.part{os.getpid()}"
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)
    return src


def _prefetch(dates, workers=None):
    """v2.8.1 — download all missing days CONCURRENTLY.

    A full day of type=details is ~17 MB and takes Savant ~30-40s to build, so
    a cold 21-day window took ~12 minutes one-at-a-time; long enough that runs
    looked hung and got killed, which is what left the board in PROXY mode.
    Downloads are independent, so we overlap them (modestly — Savant is a free
    service and we are not trying to hammer it).
    """
    todo = [d for d in dates
            if not _cache_header_ok(os.path.join(CACHE, f"{d}.csv.gz"))]
    if not todo:
        return {}
    n = workers or min(PREFETCH_WORKERS, len(todo))
    print(f"  [statcast] fetching {len(todo)} uncached day(s) with {n} workers "
          f"(~{FETCH_TIMEOUT}s each; this is the slow part of a cold run)")
    t0 = time.time()
    os.makedirs(CACHE, exist_ok=True)
    with ThreadPoolExecutor(max_workers=n) as ex:
        out = dict(zip(todo, ex.map(_ensure_day_cached, todo)))
    print(f"  [statcast] fetch phase done in {time.time() - t0:.0f}s")
    return out


def _fetch_day(date_str):
    """Return list of dict rows for one date, using validated disk cache.

    Source order: disk cache -> Baseball Savant CSV -> MLB Stats API play-by-play.
    A day is only written to cache once it parses as real data, so a blocked
    (or wrong-shaped) response can never poison later runs.
    """
    path = os.path.join(CACHE, f"{date_str}.csv.gz")
    if not os.path.exists(path) or not _cache_header_ok(path):
        if _ensure_day_cached(date_str) is None:
            return []
    rows, valid = _read_cached(path)
    return rows if valid else []


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
    from_savant, from_mlbapi = 0, 0          # v2.8 — provenance per day
    # v2.8.1 — snapshot what was already on disk BEFORE the concurrent prefetch
    # fills it, or every day would report as "cached" and days_fetched stuck at 0.
    was_cached = {d: _cache_header_ok(os.path.join(CACHE, f"{d}.csv.gz"))
                  for d in dates}
    _prefetch(dates)                          # concurrent cold-cache fill
    for d in dates:
        before = was_cached[d]
        rows = _fetch_day(d)
        after = os.path.exists(os.path.join(CACHE, f"{d}.csv.gz"))
        if before and after:
            cached += 1
        elif after:
            fetched += 1
        else:
            failed += 1
        if rows:
            if rows[0].get(SRC_COL) == "mlbapi":
                from_mlbapi += 1
            else:
                from_savant += 1
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
            # v2.8 — None (not 0.0) when the source can't supply it, so
            # shrinkage skips the key instead of pinning everyone at .000.
            "xwobacon": rate(b["xw"], b["xw_n"]) if b["xw_n"] else None,
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
        "hr_per_barrel": rate(tot_hr_b, tb[1]),
        "bbe_total": tb[0],
    }
    # v2.8 — only publish a league xwOBAcon when the window actually had one.
    # An explicit 0.0 here used to override build.py's LEAGUE_DEFAULT prior and
    # drag every hitter's shrunk xwOBAcon to .000.
    if tot_xw_n:
        league["xwobacon"] = rate(tot_xw, tot_xw_n)
    # v2.8 — which feed actually produced this window. "mlbapi" means Savant was
    # unreachable and we rebuilt shape from statsapi play-by-play: everything
    # works except xwOBAcon (regressed to prior) and barrels are recomputed
    # from EV/LA rather than read from Savant's own classification.
    source = ("savant" if from_savant and not from_mlbapi else
              "mlbapi" if from_mlbapi and not from_savant else
              "mixed" if from_mlbapi else "none")
    meta = {"window_days": days, "recent_days": recent_days,
            "start": dates[-1], "end": end_date,
            "days_fetched": fetched, "days_cached": cached,
            "days_failed": failed,
            "source": source,
            "days_savant": from_savant, "days_mlbapi": from_mlbapi,
            "xwobacon_available": from_savant > 0,
            "batters": len(batters), "pitchers": len(pitchers),
            "league": league}
    return batters, pitchers, meta


def diagnose(date_str=None):
    """v2.8 — one-screen answer to 'why is the board in PROXY mode?'.

    Run: python3 statcast.py --diag [YYYY-MM-DD]
    """
    day = date_str or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    say = lambda *a: print(*a, flush=True)     # never buffer a progress line
    say(f"Statcast source diagnosis for {day}\n" + "-" * 46)

    say(f"[1/3] Baseball Savant CSV endpoint  "
        f"({len(SAVANT_VARIANTS)} query variants, up to {DIAG_TIMEOUT}s each —"
        f" a slow reply here is normal, Savant builds the CSV on demand)")
    ok_savant = False
    season = day[:4]
    for vi, tpl in enumerate(SAVANT_VARIANTS, 1):
        url = tpl.format(d=day, season=season)
        label = ("full canonical params", "pybaseball params", "minimal params (v2.7)")[vi - 1]
        say(f"  variant {vi}/{len(SAVANT_VARIANTS)} ({label}): requesting…")
        t0 = time.time()
        try:
            req = Request(url, headers=FETCH_HEADERS)
            with urlopen(req, timeout=DIAG_TIMEOUT) as r:
                status = getattr(r, "status", None) or r.getcode()
                body = _decode_body(r.read(), r.headers)
            dur = time.time() - t0
            good = _looks_valid(body)
            head = body.lstrip().splitlines()[0][:70] if body.strip() else "<empty>"
            say(f"    -> HTTP {status} in {dur:.1f}s, {len(body)} chars, valid_csv={good}"
                f"\n       first line: {head}")
            if good:
                ok_savant = True
                break
        except Exception as e:
            say(f"    -> FAILED after {time.time() - t0:.1f}s — {type(e).__name__}: {e}")

    say("[2/3] MLB Stats API fallback")
    ok_api = False
    try:
        say("  fetching schedule…")
        pks = _game_pks(day, verbose=True)
        say(f"    -> schedule OK, {len(pks)} completed games")
        if not pks:
            say("       (0 usable games — if that date HAD games, the status "
                "filter is the\n        problem, not the feed; the line above "
                "shows what was skipped)")
        if pks:
            say(f"  pulling play-by-play for game {pks[0]}…")
            rows = _rows_from_game(pks[0])
            bip = sum(1 for r in rows if r["type"] == "X")
            ev = sum(1 for r in rows if r["launch_speed"] != "")
            say(f"    -> {len(rows)} pitches, {bip} in play, {ev} with exit velocity")
            ok_api = ev > 0
        else:
            ok_api = True                      # legitimate off-day
    except Exception as e:
        say(f"    -> FAILED — {type(e).__name__}: {e}")

    say("[3/3] Local cache")
    if os.path.isdir(CACHE):
        files = sorted(f for f in os.listdir(CACHE) if f.endswith(".csv.gz"))
        newest = files[-1][:-7] if files else "none"
        say(f"  {len(files)} cached day(s); newest = {newest}")
        stale = (files and
                 newest < (dt.date.today() - dt.timedelta(days=2)).isoformat())
        if stale:
            say("  NOTE: cache is stale — a fresh window needs live fetches.")
    else:
        say("  no cache directory yet")

    say("-" * 46)
    if ok_savant:
        say("VERDICT: Savant is reachable. Shape layer should build normally.")
    elif ok_api:
        say("VERDICT: Savant is DOWN/blocked, but the MLB Stats API fallback "
            "works.\n         build.py will produce a real shape layer "
            "(source=mlbapi):\n         xwOBAcon regresses to the league "
            "prior, everything else is live.\n         Just run: python3 build.py")
    else:
        say("VERDICT: BOTH sources failed. Check network/DNS/proxy — this is "
            "not a\n         code problem. The board will stay in PROXY mode "
            "until one returns.")
    return ok_savant, ok_api


if __name__ == "__main__":
    import sys
    if "--diag" in sys.argv:
        rest = [a for a in sys.argv[1:] if not a.startswith("-")]
        diagnose(rest[0] if rest else None)
        sys.exit(0)
    end = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 21
    b, p, m = load_window(end, days)
    print(m)
    # show a few leaders by air_pull_rate
    top = sorted(b.items(), key=lambda kv: kv[1]["air_pull_rate"], reverse=True)[:8]
    for bid, f in top:
        print(bid, f"air_pull={f['air_pull_rate']:.3f}", f"barrel={f['barrel_rate']:.3f}",
              f"z_ct={f['z_contact_rate']:.3f}", f"ev90={f['ev90']}", f"bbe={f['bbe']}")
