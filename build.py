#!/usr/bin/env python3
"""
Daily MLB Home Run pick builder — v2.3 (odds-free, free data only).

v2.3 fine-tune (not a rebuild). The clean rule:
  rank CAUSES first -> choose a CAPTURE method -> lock hitters whose
  batted-ball SHAPE (air-pull / barrel) can actually capture the cause.

  Old leg: good pitcher fade + good season power  = playable
  New leg: good cause + correct capture + role fit + air-pull/barrel fit

Key changes vs v2.2:
  - Re-weighted 100-pt score: Pitcher 25 / Hitter SHAPE 40 / Env 15 / Lineup 12 / Source 8
  - Real Statcast layer (Baseball Savant via statcast.py): air-pull, barrel,
    hard-hit, fly-ball rates for hitters; barrel/fly-ball/HR allowed for pitchers.
    No Statcast confirmation => hitter shape is capped (season power can't fake elite shape).
  - Role eligibility GATES (a high score is not automatically a bet) + hard caps
  - Elite-hitter override lane (great bat can beat a neutral spot)
  - Cause-first board with capture-method recommendation per cause
  - Harder suppressive-park handling (warning affects ROLE, not just text)
  - Four-outcome audit template

Data sources (all free, no key):
  MLB Stats API   -> schedule, probables, lineups, season stats, bat side
  Baseball Savant -> Statcast batted-ball data (statcast.py)
  Open-Meteo      -> weather

NOTE: per user direction, unconfirmed lineups are NOT hard-gated out of core;
they still flow as candidates (confirmed status is shown, and slot still scores).

Stdlib only. Run:  python3 build.py [--date YYYY-MM-DD] [--window 21] [--no-statcast]
"""

import json
import sys
import os
import datetime as dt
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor

try:
    import statcast as sc
except Exception:
    sc = None

API = "https://statsapi.mlb.com/api/v1"
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
LEADER_LIMIT = 250


# --------------------------------------------------------------------------- #
# HTTP / parse helpers
# --------------------------------------------------------------------------- #
def get(url):
    req = Request(url, headers={"User-Agent": "mlb-hr-tool/2.3"})
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def num(x, default=0.0):
    try:
        if x in (None, "", "-.--", ".---"):
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def load_parks():
    with open(os.path.join(DATA, "parks.json")) as f:
        raw = json.load(f)["parks"]
    idx = {}
    for p in raw:
        for n in p["names"]:
            idx[n.lower().strip()] = p
    return idx


def park_for(venue_name, parks):
    return parks.get(venue_name.lower().strip()) if venue_name else None


# --------------------------------------------------------------------------- #
# MLB Stats API adapters
# --------------------------------------------------------------------------- #
def fetch_schedule(date):
    url = (f"{API}/schedule?sportId=1&date={date}"
           "&hydrate=probablePitcher,team,venue")
    games = []
    for d in get(url).get("dates", []):
        for g in d.get("games", []):
            t = g["teams"]
            games.append({
                "gamePk": g["gamePk"], "gameDate": g["gameDate"],
                "status": g["status"]["detailedState"],
                "venue": g.get("venue", {}).get("name"),
                "home": _team(t["home"]), "away": _team(t["away"]),
            })
    return games


def _team(side):
    return {"id": side["team"]["id"], "name": side["team"]["name"],
            "abbr": side["team"].get("abbreviation", ""),
            "pitcher": (lambda p: {"id": p["id"], "name": p["fullName"]}
                        if p else None)(side.get("probablePitcher"))}


def fetch_hitter_pool(season):
    url = (f"{API}/stats?stats=season&group=hitting&season={season}"
           f"&sportId=1&limit={LEADER_LIMIT}&sortStat=homeRuns")
    data = get(url)
    splits = data["stats"][0]["splits"] if data.get("stats") else []
    pool = {}
    for s in splits:
        st, team = s["stat"], s.get("team", {})
        pid = s["player"]["id"]
        pool[pid] = {"id": pid, "name": s["player"]["fullName"],
                     "teamId": team.get("id"),
                     "hr": int(num(st.get("homeRuns"))),
                     "pa": int(num(st.get("plateAppearances"))),
                     "avg": num(st.get("avg")), "slg": num(st.get("slg"))}
    return pool


def fetch_pitcher_stats(pid, season):
    try:
        data = get(f"{API}/people/{pid}/stats?stats=season&season={season}&group=pitching")
        sp = data["stats"][0]["splits"]
        if not sp:
            return None
        st = sp[-1]["stat"]
        ip = num(st.get("inningsPitched"))
        bf = (num(st.get("atBats")) + num(st.get("baseOnBalls"))
              + num(st.get("hitByPitch")) + num(st.get("sacFlies")))
        return {"id": pid, "ip": ip, "hr": int(num(st.get("homeRuns"))),
                "k": int(num(st.get("strikeOuts"))), "bf": bf,
                "slg_allowed": num(st.get("slg")), "throws": None,
                "groundOuts": num(st.get("groundOuts")),
                "airOuts": num(st.get("airOuts"))}
    except Exception:
        return None


def fetch_lineup(game_pk):
    try:
        data = get(f"{API}/game/{game_pk}/boxscore?fields=teams,away,home,team,id,battingOrder")
        out = {}
        for side in ("away", "home"):
            t = data["teams"][side]
            out[t["team"]["id"]] = {pid: i + 1
                                    for i, pid in enumerate(t.get("battingOrder", [])[:9])}
        return out
    except Exception:
        return {}


def fetch_people(player_ids):
    """bat side + throw hand."""
    if not player_ids:
        return {}
    ids = ",".join(str(p) for p in player_ids)
    try:
        data = get(f"{API}/people?personIds={ids}&fields=people,id,batSide,pitchHand,code")
        return {p["id"]: {"bats": p.get("batSide", {}).get("code", ""),
                          "throws": p.get("pitchHand", {}).get("code", "")}
                for p in data.get("people", [])}
    except Exception:
        return {}


def fetch_weather(park, game_iso):
    if not park:
        return None
    try:
        when = dt.datetime.fromisoformat(game_iso.replace("Z", "+00:00"))
        ds = when.strftime("%Y-%m-%d")
        q = urlencode({"latitude": park["lat"], "longitude": park["lon"],
                       "hourly": "temperature_2m,wind_speed_10m,precipitation_probability",
                       "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                       "start_date": ds, "end_date": ds, "timezone": "UTC"})
        h = get(f"https://api.open-meteo.com/v1/forecast?{q}").get("hourly", {})
        times = h.get("time", [])
        tgt = when.strftime("%Y-%m-%dT%H:00")
        i = times.index(tgt) if tgt in times else (len(times) // 2 if times else None)
        if i is None:
            return None
        return {"temp_f": h["temperature_2m"][i], "wind_mph": h["wind_speed_10m"][i],
                "precip_pct": h.get("precipitation_probability", [None] * (i + 1))[i]}
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# v2.3 scoring
# --------------------------------------------------------------------------- #
def score_pitcher(p, scp):
    """Pitcher Cause: 0-25. scp = statcast pitcher dict or None."""
    if not p or p["ip"] < 1:
        return 0.0, "?", ["No pitcher stats available"]
    flags, pts = [], 0.0
    hr9 = p["hr"] * 9.0 / p["ip"] if p["ip"] else 0
    krate = p["k"] / p["bf"] if p["bf"] else 0
    fb_lean = p["airOuts"] / (p["airOuts"] + p["groundOuts"]) if (p["airOuts"] + p["groundOuts"]) else 0.5
    hrpa = scp["hr_per_pa_allowed"] if scp else 0
    barrel_a = scp["barrel_allowed_rate"] if scp else None
    fb_a = scp["fb_allowed_rate"] if scp else None

    # HR allowed (9) — season HR/9 or Statcast HR/PA
    if hr9 >= 1.7 or hrpa >= 0.045:
        pts += 9; flags.append(f"Elevated HR allowed (HR/9 {hr9:.2f})")
    elif hr9 >= 1.4 or hrpa >= 0.037:
        pts += 7; flags.append(f"High HR allowed (HR/9 {hr9:.2f})")
    elif hr9 >= 1.1 or hrpa >= 0.030:
        pts += 5; flags.append(f"Avg+ HR allowed (HR/9 {hr9:.2f})")
    elif hr9 >= 0.9:
        pts += 2.5
    else:
        flags.append(f"Suppresses HR (HR/9 {hr9:.2f})")

    # Barrel allowed (7) — Statcast, else SLG-allowed proxy
    if barrel_a is not None:
        if barrel_a >= 0.10:
            pts += 7; flags.append(f"Barrels up ({barrel_a*100:.0f}% allowed)")
        elif barrel_a >= 0.085:
            pts += 5.5; flags.append(f"Above-avg barrels allowed ({barrel_a*100:.0f}%)")
        elif barrel_a >= 0.07:
            pts += 4
        elif barrel_a >= 0.055:
            pts += 2.5
        else:
            pts += 1
    else:
        slg = p["slg_allowed"]
        pts += 5 if slg >= 0.47 else 3.5 if slg >= 0.43 else 2 if slg >= 0.39 else 1
        if slg >= 0.43:
            flags.append(f"Hard contact allowed (.{int(slg*1000):03d} SLG)")

    # Fly-ball allowed (5) — Statcast, else air/ground lean
    if fb_a is not None:
        pts += 5 if fb_a >= 0.42 else 4 if fb_a >= 0.38 else 2.5 if fb_a >= 0.34 else 1
        if fb_a >= 0.38:
            flags.append(f"Fly-ball prone ({fb_a*100:.0f}% air allowed)")
    else:
        pts += 5 if fb_lean >= 0.55 else 3.5 if fb_lean >= 0.50 else 2 if fb_lean >= 0.45 else 1

    # Low K (4)
    if 0 < krate < 0.16:
        pts += 4; flags.append(f"Low K rate ({krate*100:.0f}%)")
    elif 0 < krate < 0.20:
        pts += 2.5
    elif krate >= 0.27:
        flags.append(f"High K rate ({krate*100:.0f}%)")
    else:
        pts += 1.5

    pts = max(0.0, min(25.0, pts))
    grade = ("A+" if pts >= 20 else "A" if pts >= 17 else "B" if pts >= 13.5
             else "C" if pts >= 10 else "Pass")
    return round(pts, 1), grade, flags


FAM_NAME = {"FB": "fastballs", "BRK": "breaking balls", "OFF": "offspeed"}


def pitch_mix_fit(scb, scp):
    """v2.4 — does THIS hitter damage what THIS pitcher throws?

    Measured RELATIVE to the hitter's own baseline so it captures matchup, not
    raw power (power is already scored in the barrel/air-pull slots):

        idx[family] = barrel_rate_vs_family / hitter_overall_barrel_rate
        fit         = Σ pitcher_usage[family] × idx[family]

    1.0 = neutral (handles this arsenal like everything else); >1 = punishes
    what this pitcher leans on; <1 = poor fit. Families without enough BBE are
    treated as neutral (1.0). Returns (fit, detail), or (None, None) when no
    family has a usable sample (then the caller keeps the old synergy logic).
    """
    if not scb or not scp or not scp.get("mix"):
        return None, None
    overall = scb.get("barrel_rate") or 0.0
    if overall <= 0:
        return None, None
    by = scb.get("by_fam", {})
    fit, parts, observed = 0.0, [], 0
    for fam, usage in scp["mix"].items():
        fd = by.get(fam)
        if fd and fd["bbe"] >= 5:                  # enough BBE vs this family
            idx = min(2.2, max(0.3, (fd["barrel"] / fd["bbe"]) / overall))
            observed += 1
        else:
            idx = 1.0                              # no info → neutral, not power
        fit += usage * idx
        parts.append((fam, usage, idx))
    if observed == 0:
        return None, None
    dom = max(parts, key=lambda x: x[1])
    detail = (f"{dom[1]*100:.0f}% {FAM_NAME.get(dom[0], dom[0])}, "
              f"rel {dom[2]:.2f} → mix-fit {fit:.2f}x")
    return round(fit, 2), detail


def score_hitter(h, scb, scp, bats, throws):
    """Hitter HR Shape: 0-40. Returns (score, reasons, shape_source, pull_fit, mix_fit)."""
    iso = max(0.0, h["slg"] - h["avg"])
    reasons = []
    if scb:                                   # Statcast shape (preferred)
        pts = 0.0
        ap, br, hh = scb["air_pull_rate"], scb["barrel_rate"], scb["hard_hit_rate"]
        # Air-pull (12) — top HR signal
        if ap >= 0.20:
            pts += 12; reasons.append(f"Elite air-pull ({ap*100:.0f}%)")
        elif ap >= 0.15:
            pts += 10; reasons.append(f"Strong air-pull ({ap*100:.0f}%)")
        elif ap >= 0.12:
            pts += 8; reasons.append(f"Good air-pull ({ap*100:.0f}%)")
        elif ap >= 0.09:
            pts += 5
        elif ap >= 0.06:
            pts += 3
        else:
            pts += 1; reasons.append(f"Low air-pull ({ap*100:.0f}%)")
        # Barrels (9)
        if br >= 0.15:
            pts += 9; reasons.append(f"Elite barrel rate ({br*100:.0f}%)")
        elif br >= 0.12:
            pts += 7.5; reasons.append(f"Strong barrels ({br*100:.0f}%)")
        elif br >= 0.09:
            pts += 6; reasons.append(f"Above-avg barrels ({br*100:.0f}%)")
        elif br >= 0.06:
            pts += 4
        elif br >= 0.04:
            pts += 2
        else:
            pts += 0.5
        # Hard-hit (6)
        pts += (6 if hh >= 0.50 else 5 if hh >= 0.45 else 4 if hh >= 0.40
                else 2.5 if hh >= 0.35 else 1)
        if hh >= 0.45:
            reasons.append(f"Hard-hit {hh*100:.0f}%")
        # Matchup fit (6): platoon hand + real pitch-mix fit (v2.4)
        fit = 3.0
        if bats and throws:
            if bats != throws and bats in ("L", "R"):
                fit += 1.5; reasons.append(f"Platoon edge ({bats} vs {throws}HP)")
            elif bats == throws:
                fit -= 1.0
            elif bats == "S":
                fit += 0.5
        mix_fit, mix_detail = pitch_mix_fit(scb, scp)
        if mix_fit is not None:
            fit += max(-2.5, min(3.0, (mix_fit - 1.0) * 5.0))
            if mix_fit >= 1.20:
                reasons.append(f"Pitch-mix fit: damages this arsenal ({mix_detail})")
            elif mix_fit < 0.85:
                reasons.append(f"Poor pitch-mix fit ({mix_detail})")
        elif scp and ap >= 0.12 and (scp["fb_allowed_rate"] >= 0.40 or scp["barrel_allowed_rate"] >= 0.09):
            fit += 2.0; reasons.append("Cause-capture synergy (air-pull bat vs fly-ball/barrel-prone arm)")
        pts += max(0.0, min(6.0, fit))
        # Platoon/season ISO (4)
        pts += (4 if iso >= 0.25 else 3 if iso >= 0.20 else 2 if iso >= 0.16
                else 1 if iso >= 0.13 else 0.5)
        # Recent form (3)
        rb, rhr = scb["recent_barrels"], scb["recent_hr"]
        if rhr >= 2 or rb >= 4:
            pts += 3; reasons.append(f"Hot ({rhr} HR / {rb} barrels last 7d)")
        elif rb >= 2:
            pts += 2
        elif rb >= 1:
            pts += 1
        return (round(max(0.0, min(40.0, pts)), 1), reasons, "statcast",
                (ap >= 0.12 or br >= 0.10), mix_fit)

    # ----- proxy fallback (no Statcast): season power only, CAPPED at 30/40 -----
    hr_rate = h["hr"] / h["pa"] if h["pa"] else 0
    pts = 0.0
    if hr_rate >= 0.060:
        pts += 14; reasons.append(f"Proxy: elite HR rate (1/{1/hr_rate:.0f} PA)")
    elif hr_rate >= 0.045:
        pts += 11; reasons.append(f"Proxy: strong HR rate")
    elif hr_rate >= 0.032:
        pts += 7
    elif hr_rate >= 0.022:
        pts += 4
    if iso >= 0.25:
        pts += 10; reasons.append(f"Proxy: elite ISO ({iso:.3f})")
    elif iso >= 0.20:
        pts += 7
    elif iso >= 0.16:
        pts += 5
    elif iso >= 0.13:
        pts += 2
    pts += 6 if h["slg"] >= 0.52 else 4 if h["slg"] >= 0.46 else 2 if h["slg"] >= 0.41 else 0
    reasons.append("No Statcast shape — score capped (season-power proxy)")
    return round(min(30.0, pts), 1), reasons, "proxy", False, None


def score_environment(park, weather, bat):
    reasons, warnings, pts = [], [], 0.0
    hf = None
    if park:
        hf = park["hr"]
        if bat == "L":
            hf = park.get("hr_l", hf)
        elif bat == "R":
            hf = park.get("hr_r", hf)
        if hf >= 110:
            pts += 9; reasons.append(f"Elite HR park ({hf})")
        elif hf >= 105:
            pts += 7; reasons.append(f"Hitter-friendly park ({hf})")
        elif hf >= 100:
            pts += 5; reasons.append(f"Slightly + park ({hf})")
        elif hf >= 95:
            pts += 4
        elif hf >= 90:
            pts += 2; warnings.append(f"Pitcher park ({hf})")
        else:
            pts += 1; warnings.append(f"Strong pitcher park ({hf})")
        if park.get("note"):
            reasons.append(park["note"])
    else:
        pts += 4; warnings.append("Park factor unknown")

    roof = park["roof"] if park else "unknown"
    if roof in ("dome", "retractable"):
        pts += 3; reasons.append(f"{roof.title()} roof (weather neutralized)")
    elif weather:
        t = weather.get("temp_f")
        if t is not None:
            if t >= 85:
                pts += 5; reasons.append(f"Hot ({t:.0f}°F)")
            elif t >= 75:
                pts += 4; reasons.append(f"Warm ({t:.0f}°F)")
            elif t >= 65:
                pts += 3
            elif t >= 50:
                pts += 2
            else:
                warnings.append(f"Cold ({t:.0f}°F)")
        if (weather.get("wind_mph") or 0) >= 15:
            warnings.append(f"High wind {weather['wind_mph']:.0f} mph (dir not modeled)")
        if (weather.get("precip_pct") or 0) >= 60:
            warnings.append(f"Rain risk {weather['precip_pct']:.0f}%")
    else:
        pts += 3; warnings.append("Weather unavailable")
    return round(max(0.0, min(15.0, pts)), 1), reasons, warnings, hf


def score_lineup(slot, confirmed):
    """0-12. Unconfirmed is NOT hard-gated (per direction) — neutral default."""
    if slot is None:
        return 6.0, [], (["Lineup not confirmed"] if not confirmed else [])
    if slot <= 2:
        return 12.0, [f"Top of order (#{slot})"], []
    if slot <= 4:
        return 11.0, [f"Heart of order (#{slot})"], []
    if slot <= 6:
        return 8.0, [f"Mid order (#{slot})"], []
    return 5.0, [], [f"Bottom of order (#{slot})"]


def score_confidence(h40, pcause):
    if h40 >= 30 and pcause >= 14:
        return 5.0, ["Model: elite shape + clear cause"]
    if h40 >= 26 or pcause >= 17:
        return 4.0, ["Model: one strong signal"]
    if h40 >= 20:
        return 3.0, []
    return 2.0, []


def assign_role(total, h40, pgrade, pcause, hf, pull_fit, slot, confirmed,
                env_pts, warnings):
    """
    v2.3 role eligibility — gates, not raw score. Returns (role, notes).
    (Unconfirmed lineups are allowed through per user direction.)
    """
    notes = []
    major_park_pen = (hf is not None and hf <= 92)
    weather_pen = any(w.startswith(("Cold", "Rain")) for w in warnings)
    neutral_env = env_pts >= 6
    elite_hitter = h40 >= 32 and neutral_env and (slot is None or slot <= 4)
    top5 = slot is not None and slot <= 5
    clean_env = (hf is None or hf >= 95) and not weather_pen

    # ---- hard caps first ----
    if h40 < 22:
        notes.append("Hitter shape <22 → capped at Longshot")
        return "Longshot" if (62 <= total) else "Watchlist", notes
    if major_park_pen and not pull_fit:
        notes.append("Suppressive park + no pull/barrel fit → Watchlist")
        return "Watchlist", notes
    if pgrade == "Pass" and not elite_hitter:
        return "Pass", notes

    # ---- positive lanes ----
    # Locked Core: strong shape + role + (real cause OR elite bat) + clean env
    if h40 >= 32 and top5 and (pgrade in ("A+", "A") or elite_hitter) and clean_env:
        return "Locked Core", notes
    # Cause Satellite: strong cause + decent hitter
    if pgrade in ("A+", "A") and 22 <= h40 < 32:
        return "Cause Satellite", notes
    # Power Satellite: elite hitter, weak/moderate cause
    if elite_hitter and pgrade in ("B", "C", "Pass"):
        notes.append("Elite-hitter override (beats a neutral spot)")
        return "Power Satellite", notes
    # Core (shape + cause but maybe not top-5/clean)
    if h40 >= 30 and pgrade in ("A+", "A", "B"):
        return "Core", notes
    # Longshot: real cause, mid total
    if pgrade in ("A+", "A", "B") and total >= 62:
        return "Longshot", notes
    if total >= 70:
        return "Watchlist", notes
    return "Pass", notes


def tier_label(total):
    return ("Elite Core" if total >= 85 else "Core" if total >= 78
            else "Satellite" if total >= 70 else "Longshot" if total >= 62 else "Pass")


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build(date, window=21, use_statcast=True):
    season = date[:4]
    parks = load_parks()
    games = fetch_schedule(date)
    pool = fetch_hitter_pool(season)

    # Statcast window (cause-capture shape layer)
    scb, scp_map, scmeta = {}, {}, None
    if use_statcast and sc is not None:
        try:
            scb, scp_map, scmeta = sc.load_window(date, days=window)
            print(f"  statcast: {scmeta['batters']} batters, {scmeta['pitchers']} pitchers "
                  f"({scmeta['start']}..{scmeta['end']})")
        except Exception as e:
            print(f"  statcast unavailable ({e}); using proxy shape")

    # team -> context
    team_ctx, pitcher_ids = {}, set()
    for g in games:
        for side, opp in (("home", "away"), ("away", "home")):
            tid = g[side]["id"]
            op = g[opp]["pitcher"]
            if op:
                pitcher_ids.add(op["id"])
            team_ctx[tid] = {"gamePk": g["gamePk"], "gameDate": g["gameDate"],
                             "venue": g["venue"], "is_home": side == "home",
                             "team": g[side]["name"], "opp": g[opp]["name"],
                             "opp_pitcher": op}

    with ThreadPoolExecutor(max_workers=12) as ex:
        pstats_list = list(ex.map(lambda pid: fetch_pitcher_stats(pid, season), pitcher_ids))
        lineups_list = list(ex.map(fetch_lineup, [g["gamePk"] for g in games]))
        weather_list = list(ex.map(
            lambda g: fetch_weather(park_for(g["venue"], parks), g["gameDate"]), games))

    pstats = {p["id"]: p for p in pstats_list if p}
    lineups = {}
    for lu in lineups_list:
        lineups.update(lu)
    weather = {g["gamePk"]: w for g, w in zip(games, weather_list)}

    candidate_ids = [h["id"] for h in pool.values() if h["teamId"] in team_ctx]
    people = fetch_people(candidate_ids)
    pthrow = fetch_people(list(pitcher_ids))

    # pitcher cause cards
    pcards = {}
    for pid, p in pstats.items():
        s, grade, flags = score_pitcher(p, scp_map.get(pid))
        pcards[pid] = {"score": s, "grade": grade, "flags": flags,
                       "hr9": round(p["hr"] * 9.0 / p["ip"], 2) if p["ip"] else None,
                       "barrel_allowed": (scp_map.get(pid) or {}).get("barrel_allowed_rate")}

    candidates = []
    for h in pool.values():
        ctx = team_ctx.get(h["teamId"])
        if not ctx or h["pa"] < 20:
            continue
        slot = lineups.get(h["teamId"], {}).get(h["id"])
        confirmed = slot is not None
        bats = people.get(h["id"], {}).get("bats", "")
        op = ctx["opp_pitcher"]
        pc = pcards.get(op["id"]) if op else None
        pcause = pc["score"] if pc else 0
        pgrade = pc["grade"] if pc else "?"
        throws = pthrow.get(op["id"], {}).get("throws", "") if op else ""

        scb_h = scb.get(h["id"])
        h40, h_reasons, shape_src, pull_fit, mix_fit = score_hitter(
            h, scb_h, scp_map.get(op["id"]) if op else None, bats, throws)
        park = park_for(ctx["venue"], parks)
        e_pts, e_reasons, e_warn, hf = score_environment(park, weather.get(ctx["gamePk"]), bats)
        l_pts, l_reasons, l_warn = score_lineup(slot, confirmed)
        c_pts, c_reasons = score_confidence(h40, pcause)
        warnings = e_warn + l_warn

        total = round(pcause + h40 + e_pts + l_pts + c_pts, 1)
        role, role_notes = assign_role(total, h40, pgrade, pcause, hf, pull_fit,
                                        slot, confirmed, e_pts, warnings)

        # v2.4 fit tag: flag trap bats (great surface shape, wrong arsenal fit)
        # and pitch-fit bats (shape that specifically punishes this arm).
        actionable = ("Locked Core", "Core", "Mini-Stack Bat", "Cause Satellite",
                      "Power Satellite")
        fit_tag = None
        if mix_fit is not None:
            if mix_fit < 0.85 and role in ("Locked Core", "Core", "Cause Satellite"):
                fit_tag = "TRAP"
                warnings.append(f"TRAP: strong surface shape but poor pitch-type fit "
                                f"({mix_fit}x) vs this arsenal")
            elif mix_fit >= 1.25 and role in actionable:
                fit_tag = "PITCH-FIT"

        # Value longshot: an off-chalk live dog — a non-core role, but with genuine
        # HR shape, a real cause, and a fit that isn't a trap. ("The chalk guys
        # don't all go" — these are the lower-profile upside bats worth a dart.)
        value_play = (role in ("Cause Satellite", "Power Satellite", "Longshot")
                      and 62 <= total < 76 and h40 >= 25 and pull_fit
                      and pgrade in ("A+", "A", "B")
                      and (mix_fit is None or mix_fit >= 1.05) and fit_tag != "TRAP")

        # barrel→HR park-fit tiebreaker (separates GABP/Coors/short-porch carry)
        bhr_reason = None
        if scb_h and hf is not None and scb_h["barrel_rate"] >= 0.10 \
                and scb_h["air_pull_rate"] >= 0.15 and hf >= 105:
            bhr_reason = f"Barrel→HR park fit: pull-air power in a carry park ({hf})"

        reasons = []
        if op:
            reasons.append(f"vs {op['name']} — cause {pgrade} ({pcause}/25)")
            reasons += [f"Pitcher: {f}" for f in (pc["flags"][:2] if pc else [])]
        reasons += h_reasons[:4] + e_reasons[:2] + l_reasons + role_notes + c_reasons[:1]
        if value_play:
            reasons.append("VALUE longshot: real shape + cause off the chalk")
        if bhr_reason:
            reasons.append(bhr_reason)

        candidates.append({
            "id": h["id"], "name": h["name"], "team": ctx["team"], "bats": bats or "?",
            "opp": ctx["opp"], "opp_pitcher": op["name"] if op else None,
            "venue": ctx["venue"], "gamePk": ctx["gamePk"], "slot": slot,
            "confirmed": confirmed, "season_hr": h["hr"],
            "iso": round(h["slg"] - h["avg"], 3),
            "total": total, "tier": tier_label(total), "role": role,
            "shape_source": shape_src, "pull_fit": pull_fit,
            "mix_fit": mix_fit, "fit_tag": fit_tag, "value_play": value_play,
            "pitcher_grade": pgrade,
            "breakdown": {"pitcher": pcause, "hitter": h40, "environment": e_pts,
                          "lineup": l_pts, "confidence": c_pts},
            "statcast": ({k: scb[h["id"]][k] for k in
                          ("air_pull_rate", "barrel_rate", "hard_hit_rate", "fb_rate")}
                         if scb.get(h["id"]) else None),
            "reasons": reasons, "warnings": warnings, "role_notes": role_notes,
        })

    candidates.sort(key=lambda c: c["total"], reverse=True)
    causes = build_causes(games, team_ctx, pcards, weather, parks, candidates)
    structures = build_structures(candidates)
    audit = build_audit_template(candidates, causes)

    return {
        "version": "2.4",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "slate_date": date, "games": len(games),
        "confirmed_lineups": sum(1 for tid in lineups if lineups[tid]),
        "statcast": bool(scb),
        "statcast_meta": scmeta,
        "method": "v2.4: rank causes → choose capture → lock confirmed shape that "
                  "captures the cause, with pitch-mix fit (does this bat damage THIS "
                  "arsenal?) + TRAP flags. Weights P25/Hitter-shape40/Env15/Lineup12/Src8. "
                  + ("Statcast (Savant) air-pull/barrel/pitch-mix active."
                     if scb else "PROXY shape mode (no Statcast) — scores capped, no pitch-mix fit."),
        "tiers": {"Elite Core": "85+", "Core": "78-84", "Satellite": "70-77",
                  "Longshot": "62-69", "Pass": "<62"},
        "roles": ["Locked Core", "Mini-Stack Bat", "Power Satellite", "Cause Satellite",
                  "Core", "Longshot", "Watchlist", "Pass"],
        "causes": causes, "candidates": candidates, "structures": structures,
        "pitcher_board": sorted(
            [{"id": pid,
              "name": next((g[s]["pitcher"]["name"] for g in games for s in ("home", "away")
                            if g[s]["pitcher"] and g[s]["pitcher"]["id"] == pid), str(pid)),
              **c} for pid, c in pcards.items()],
            key=lambda x: x["score"], reverse=True),
        "audit_template": audit,
    }


def build_causes(games, team_ctx, pcards, weather, parks, candidates):
    """Rank slate causes (pitcher fade + environment) and recommend a capture method."""
    by_team = {}
    for c in candidates:
        by_team.setdefault(c["team"], []).append(c)
    causes = []
    for tid, ctx in team_ctx.items():
        op = ctx["opp_pitcher"]
        if not op:
            continue
        pc = pcards.get(op["id"])
        if not pc:
            continue
        park = park_for(ctx["venue"], parks)
        env_hint = park["hr"] if park else 100
        bats = sorted([b for b in by_team.get(ctx["team"], []) if b["total"] >= 62],
                      key=lambda x: x["total"], reverse=True)
        playable = [b for b in bats if b["breakdown"]["hitter"] >= 22]
        cause_score = round(pc["score"] + (env_hint - 100) * 0.25, 1)
        grade = pc["grade"]
        n = len(playable)
        if grade == "A+" and n >= 2:
            capture = "2-bat mini-stack or team-HR (strong cause, multiple bats)"
        elif grade == "A" and n >= 2:
            capture = "2-bat mini-stack (cause supports coverage)"
        elif grade in ("A+", "A", "B") and n >= 1:
            capture = "Single best bat"
        elif grade in ("A+", "A", "B"):
            capture = "Team-HR placeholder (cause strong, bat capture thin)"
        else:
            capture = "Watchlist / pass (no clear cause)"
        causes.append({
            "team": ctx["team"], "opp": ctx["opp"], "pitcher": op["name"],
            "venue": ctx["venue"], "grade": grade, "pitcher_cause": pc["score"],
            "park_hr": env_hint, "cause_score": cause_score,
            "capture_method": capture, "public_heat": False,
            "n_playable": n,
            "playable_bats": [{"name": b["name"], "total": b["total"], "role": b["role"],
                               "fit_tag": b.get("fit_tag")}
                              for b in playable[:4]],
        })
    causes.sort(key=lambda x: (x["cause_score"], x["pitcher_cause"]), reverse=True)
    # Public heat = ONLY the single chalkiest spot (top A+ cause stacked with bats).
    # Toned down so the flag means "this is THE obvious one, don't over-load it",
    # not a label on every good game.
    for c in causes:
        if c["grade"] == "A+" and c["n_playable"] >= 4:
            c["public_heat"] = True
            break
    return causes


def build_structures(cands):
    structures = {}
    actionable = {"Locked Core", "Core", "Mini-Stack Bat", "Cause Satellite",
                  "Power Satellite"}
    # 4-leg RR core — diversify by game, prefer locked/strong roles
    rr, seen = [], set()
    for c in cands:
        if c["role"] not in actionable or c["total"] < 70:
            continue
        if c["gamePk"] in seen:
            continue
        rr.append(f"{c['name']} ({c['team']}, {c['total']}, {c['role']})")
        seen.add(c["gamePk"])
        if len(rr) == 4:
            break
    if len(rr) >= 3:
        structures["rr_core_4leg"] = {"legs": rr,
            "why": "Four different games/causes (survival layer) — RR scores on cause diversity."}

    # Same-game correlated pair — same team, same pitcher, A/A+ cause
    by_team = {}
    for c in cands:
        by_team.setdefault(c["team"], []).append(c)
    best = None
    for team, members in by_team.items():
        strong = [m for m in members if m["breakdown"]["hitter"] >= 27
                  and m["pitcher_grade"] in ("A+", "A")]
        if len(strong) >= 2 and (best is None or strong[0]["total"] > best[0]["total"]):
            best = (strong[0], strong[1])
    if best:
        a, b = best
        structures["same_game_pair"] = {
            "legs": [f"{a['name']} ({a['total']})", f"{b['name']} ({b['total']})"],
            "game": f"{a['team']} vs {a['opp']}",
            "why": f"Shared cause: {a['opp_pitcher']} grades {a['pitcher_grade']}. Both "
                   f"{a['team']} bats have the shape to capture the same environment."}

    # Longshot convergence — real cause, deeper bats
    ls = [c for c in cands if c["role"] == "Longshot"][:4]
    if ls:
        structures["longshot_group"] = {
            "legs": [f"{c['name']} ({c['team']}, {c['total']})" for c in ls],
            "why": "Deeper bats with a real pitcher/park cause — bonus-only convergence."}
    return structures


def build_audit_template(cands, causes):
    """Postgame four-outcome audit scaffold (fill result fields after games)."""
    rows = [{"player": c["name"], "team": c["team"], "opp_pitcher": c["opp_pitcher"],
             "role": c["role"], "total": c["total"], "cause_grade": c["pitcher_grade"],
             "result_hr": None, "cause_fired": None, "teammate_hr": None,
             "outcome": None}
            for c in cands if c["role"] in
            ("Locked Core", "Core", "Mini-Stack Bat", "Cause Satellite", "Power Satellite")][:15]
    return {
        "legend": {"A": "cause fired + our bat hit (perfect)",
                   "B": "cause fired + teammate hit (right cause, wrong capture)",
                   "C": "cause did not fire + bat missed (true miss)",
                   "D": "cause did not fire + bat hit (override/lucky)"},
        "instructions": "After games, set result_hr / cause_fired (did target pitcher or "
                        "team produce HRs/barrels) / teammate_hr, then classify outcome A-D.",
        "rows": rows,
    }


def main():
    date = dt.date.today().isoformat()
    window, use_sc = 21, True
    if "--date" in sys.argv:
        date = sys.argv[sys.argv.index("--date") + 1]
    if "--window" in sys.argv:
        window = int(sys.argv[sys.argv.index("--window") + 1])
    if "--no-statcast" in sys.argv:
        use_sc = False
    print(f"Building HR slate v2.4 for {date} ...")
    out = build(date, window=window, use_statcast=use_sc)
    os.makedirs(DATA, exist_ok=True)
    for name in ("latest.json", f"picks-{date}.json"):
        with open(os.path.join(DATA, name), "w") as f:
            json.dump(out, f, indent=2)
    print(f"  games={out['games']} candidates={len(out['candidates'])} "
          f"confirmed_lineups={out['confirmed_lineups']} statcast={out['statcast']}")
    print("\n  Top causes:")
    for c in out["causes"][:4]:
        print(f"    {c['cause_score']:>5} {c['grade']:<2} {c['team']} vs {c['pitcher']} "
              f"→ {c['capture_method']}")
    print("\n  Top hitter picks:")
    for c in out["candidates"][:6]:
        sc_tag = "SC" if c["shape_source"] == "statcast" else "px"
        ft = f" [{c['fit_tag']}]" if c.get("fit_tag") else ""
        mf = f" mix-fit {c['mix_fit']}" if c.get("mix_fit") is not None else ""
        print(f"    {c['total']:>5} {c['role']:<15} {c['name']} ({c['team']}) "
              f"[{sc_tag} shape {c['breakdown']['hitter']}/40{mf}]{ft} vs {c['opp_pitcher']}")


if __name__ == "__main__":
    main()
