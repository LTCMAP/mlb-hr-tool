#!/usr/bin/env python3
"""
Daily MLB Home Run pick builder — v2.5 (odds-free, free data only).

v2.5 model upgrades (on top of the v2.3/v2.4 cause-capture system):
  - Directional wind: each park carries an approximate home-plate→CF bearing;
    Open-Meteo wind direction is resolved into an out/in component (cosine),
    worth up to ±3 env points at open-air parks. Wind can now RAISE a score,
    not just warn.
  - Empirical-Bayes shrinkage: all 21-day Statcast rates (hitter barrel /
    air-pull / hard-hit / FB; pitcher barrel-allowed / FB-allowed / HR-PA)
    regress toward the league window average by sample size, so a hot 10-BBE
    week can no longer masquerade as elite shape.
  - Platoon-split shape: hitter barrel/air-pull vs the *opposing pitcher's
    hand* (blended with overall shape) replaces shape-blind platoon handling.

The clean rule (v2.3):
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
import math
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
                       "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,"
                                 "precipitation_probability",
                       "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                       "start_date": ds, "end_date": ds, "timezone": "UTC"})
        h = get(f"https://api.open-meteo.com/v1/forecast?{q}").get("hourly", {})
        times = h.get("time", [])
        tgt = when.strftime("%Y-%m-%dT%H:00")
        i = times.index(tgt) if tgt in times else (len(times) // 2 if times else None)
        if i is None:
            return None
        wd = h.get("wind_direction_10m", [])
        return {"temp_f": h["temperature_2m"][i], "wind_mph": h["wind_speed_10m"][i],
                "wind_dir_deg": wd[i] if i < len(wd) else None,
                "precip_pct": h.get("precipitation_probability", [None] * (i + 1))[i]}
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# v2.3 scoring
# --------------------------------------------------------------------------- #
def score_pitcher(p, scp, lg=None):
    """Pitcher Cause: 0-25. scp = statcast pitcher dict or None."""
    lg = lg or {}
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

    # Fly-ball allowed (5) — Statcast true FB rate (league ~26%), else air/ground lean
    if fb_a is not None:
        pts += 5 if fb_a >= 0.33 else 4 if fb_a >= 0.29 else 2.5 if fb_a >= 0.25 else 1
        if fb_a >= 0.29:
            flags.append(f"Fly-ball prone ({fb_a*100:.0f}% FB allowed)")
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

    # v2.6 — luck/skill pairing on the arm:
    # (a) HR-DUE: loud contact allowed but HRs haven't followed yet → the cause
    #     is BETTER than his HR/9 shows (surface stats hide the fade).
    # (b) Hard-hit allowed ≥46% → extra loud-contact credit.
    if scp:
        hh_a = scp.get("hard_hit_allowed_rate")
        # xHR allowed from raw barrel count (immune to shrinkage flattening)
        nb_a, hr_a = scp.get("barrels_allowed"), scp.get("hr_allowed")
        if nb_a is not None and hr_a is not None:
            hpb = lg.get("hr_per_barrel") or LEAGUE_DEFAULT["hr_per_barrel"]
            due_a = nb_a * hpb - hr_a
            if due_a >= 2.5 and nb_a >= 4:
                pts += 1.5
                flags.append(f"HR-DUE: {nb_a} barrels allowed, only {hr_a} HR "
                             f"(+{due_a:.1f} xHR) — regression incoming, better "
                             "fade than HR/9 shows")
            elif due_a <= -3:
                flags.append(f"HR luck against him ({due_a:+.1f} xHR) — HR/9 "
                             "overstates the fade")
        if hh_a is not None and hh_a >= 0.46:
            pts += 1
            flags.append(f"Loud contact allowed (hard-hit {hh_a*100:.0f}%)")

    pts = max(0.0, min(25.0, pts))
    grade = ("A+" if pts >= 20 else "A" if pts >= 17 else "B" if pts >= 13.5
             else "C" if pts >= 10 else "Pass")
    return round(pts, 1), grade, flags


# v2.5 — empirical-Bayes shrinkage. Small 21-day samples regress toward the
# league rate over the same window; prior weights (in BBE) follow published
# stabilization research (batted-ball quality rates settle around 40–80 BBE;
# HR/PA is far noisier, so pitcher HR/PA gets a heavy ~350-PA prior).
SHRINK_B = {"barrel_rate": 60, "hard_hit_rate": 40, "fb_rate": 50, "air_pull_rate": 60,
            "sweet_spot_rate": 50, "xwobacon": 60}
# v2.6 — Z-Contact stabilizes fast (~60 in-zone swings); shrunk on its own
# denominator (zone_swings), not BBE.
SHRINK_B_ZSW = {"z_contact_rate": 60}
SHRINK_P = {"barrel_allowed_rate": ("barrel_rate", 70),
            "hard_hit_allowed_rate": ("hard_hit_rate", 50),
            "fb_allowed_rate": ("fly_rate", 60)}
HR_PA_PRIOR = 350  # PA weight for pitcher HR/PA shrinkage
LEAGUE_DEFAULT = {"barrel_rate": 0.085, "hard_hit_rate": 0.40, "fb_rate": 0.47,
                  "air_pull_rate": 0.18, "fly_rate": 0.27, "hr_per_pa": 0.031,
                  "z_contact": 0.82, "sweet_spot_rate": 0.335,
                  "xwobacon": 0.365, "hr_per_barrel": 0.55}


def apply_shrinkage(scb, scp_map, league):
    """Regress windowed Statcast rates toward league means by sample size.

    A 8-BBE hitter showing a 25% barrel rate is mostly noise; after shrinkage
    he reads ~10-11%, so a hot week can no longer buy 'elite shape'. Full-time
    hitters (80+ BBE over 21d) keep most of their observed rate. Raw values
    are preserved under ['raw'] for the dashboard/debugging.
    """
    lg = {**LEAGUE_DEFAULT, **(league or {})}
    # v2.8 — a league block may carry an explicit 0.0 for a metric the source
    # couldn't supply (e.g. xwOBAcon on the MLB-API fallback). A 0.0 prior is
    # not "no information", it's a wrong one: it drags every hitter to .000.
    for _k in ("z_contact", "xwobacon", "barrel_rate", "hard_hit_rate",
               "fb_rate", "air_pull_rate", "fly_rate", "sweet_spot_rate"):
        if not lg.get(_k):
            lg[_k] = LEAGUE_DEFAULT[_k]                    # older/partial league blocks
    for b in scb.values():
        n = b["bbe"]
        b["raw"] = {k: b[k] for k in SHRINK_B if k in b}
        for k, kw in SHRINK_B.items():
            if b.get(k) is not None:
                nk = b.get("xw_n", n) if k == "xwobacon" else n   # v2.6 own denom
                b[k] = round((b[k] * nk + lg.get(k, LEAGUE_DEFAULT[k]) * kw) / (nk + kw), 4)
        for k, kw in SHRINK_B_ZSW.items():                 # v2.6 — zone-swing denom
            if b.get(k) is not None:
                nz = b.get("zone_swings", 0)
                b[k] = round((b[k] * nz + lg["z_contact"] * kw) / (nz + kw), 4)
    for p in scp_map.values():
        n = p["bbe"]
        for k, (lk, kw) in SHRINK_P.items():
            p[k] = round((p[k] * n + lg[lk] * kw) / (n + kw), 4)
        p["hr_per_pa_allowed"] = round(
            (p["hr_per_pa_allowed"] * p["pa"] + lg["hr_per_pa"] * HR_PA_PRIOR)
            / (p["pa"] + HR_PA_PRIOR), 4)


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


def score_hitter(h, scb, scp, bats, throws, lg=None):
    """Hitter HR Shape: 0-40. Returns (score, reasons, shape_source, pull_fit, mix_fit, due_hr).

    v2.6 weights: air-pull 11 / barrel 8 / zone-damage 7 (hard-hit × Z-Contact)
    / matchup 6 / contact quality 3 / recent form 3 / HR-luck 'due' 2.
    """
    iso = max(0.0, h["slg"] - h["avg"])
    lg = lg or {}
    reasons = []
    if scb:                                   # Statcast shape (preferred)
        pts = 0.0
        ap, br, hh = scb["air_pull_rate"], scb["barrel_rate"], scb["hard_hit_rate"]
        # v2.5 — platoon-split shape: if we know the opposing hand, blend the
        # hitter's shape vs THAT hand into his overall (shrunk) rate. Raw
        # hand-split counts + a 30-BBE overall prior, so a thin split can't
        # dominate but a real one (e.g. barrels only vs LHP) reshapes the score.
        vh = scb.get("vs_hand", {}).get(throws) if throws in ("L", "R") else None
        if vh and vh["bbe"] >= 10:
            m = 30.0
            ap_h = (vh["pulled_air"] + ap * m) / (vh["bbe"] + m)
            br_h = (vh["barrels"] + br * m) / (vh["bbe"] + m)
            if br_h >= br * 1.2 or ap_h >= ap * 1.25:
                reasons.append(f"Platoon shape: spikes vs {throws}HP "
                               f"(barrel {br_h*100:.0f}%, air-pull {ap_h*100:.0f}%)")
            elif br_h <= br * 0.8 and ap_h <= ap * 0.85:
                reasons.append(f"Platoon shape fades vs {throws}HP")
            ap, br = ap_h, br_h
        # Air-pull (11) — top HR signal (pulled air balls are ~17.5% of contact
        # but ~66% of HR). v2.5: true spray-angle pull (outer third); league
        # ~18% of BBE, elite pullers ~28%+.
        if ap >= 0.28:
            pts += 11; reasons.append(f"Elite air-pull ({ap*100:.0f}%)")
        elif ap >= 0.23:
            pts += 9; reasons.append(f"Strong air-pull ({ap*100:.0f}%)")
        elif ap >= 0.19:
            pts += 7; reasons.append(f"Good air-pull ({ap*100:.0f}%)")
        elif ap >= 0.15:
            pts += 4.5
        elif ap >= 0.11:
            pts += 2.5
        else:
            pts += 1; reasons.append(f"Low air-pull ({ap*100:.0f}%)")
        # Barrels (8)
        if br >= 0.15:
            pts += 8; reasons.append(f"Elite barrel rate ({br*100:.0f}%)")
        elif br >= 0.12:
            pts += 6.5; reasons.append(f"Strong barrels ({br*100:.0f}%)")
        elif br >= 0.09:
            pts += 5.5; reasons.append(f"Above-avg barrels ({br*100:.0f}%)")
        elif br >= 0.06:
            pts += 3.5
        elif br >= 0.04:
            pts += 2
        else:
            pts += 0.5
        # Zone-damage (7) — v2.6: hard-hit PAIRED with in-zone contact. Hard
        # contact only cashes if the bat actually meets hittable pitches;
        # HH% + Z-Contact% together track HR/FB far better than either alone
        # (r≈0.8 with barrels in published fits). League HH ~40%, Z-Ct ~82%.
        zc = scb.get("z_contact_rate")
        if zc is not None and hh >= 0.50 and zc >= 0.85:
            pts += 7; reasons.append(
                f"Zone-damage elite: hard-hit {hh*100:.0f}% + Z-contact {zc*100:.0f}%")
        elif zc is not None and hh >= 0.45 and zc >= 0.82:
            pts += 5.5; reasons.append(
                f"Zone-damage: hard-hit {hh*100:.0f}% + Z-contact {zc*100:.0f}%")
        elif hh >= 0.50:
            pts += 5; reasons.append(f"Hard-hit {hh*100:.0f}%")
        elif hh >= 0.45:
            pts += 4.5; reasons.append(f"Hard-hit {hh*100:.0f}%")
        elif hh >= 0.40:
            pts += 3 + (0.5 if (zc or 0) >= 0.86 else 0)
        elif hh >= 0.35:
            pts += 2
        else:
            pts += 1
        if zc is not None and zc < 0.75:
            reasons.append(f"Whiff risk: Z-contact only {zc*100:.0f}%")
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
        elif scp and ap >= 0.18 and (scp["fb_allowed_rate"] >= 0.30 or scp["barrel_allowed_rate"] >= 0.09):
            fit += 2.0; reasons.append("Cause-capture synergy (air-pull bat vs fly-ball/barrel-prone arm)")
        pts += max(0.0, min(6.0, fit))
        # Contact quality (3) — v2.6: xwOBA-on-contact (EV+LA expectation)
        # backstopped by season ISO. xwOBAcon strips defense/park/sequencing
        # noise out of "is this contact actually dangerous".
        xw = scb.get("xwobacon") or 0.0
        if xw >= 0.42 or iso >= 0.25:
            pts += 3
            if xw >= 0.42:
                reasons.append(f"Dangerous contact (xwOBAcon .{int(xw*1000):03d})")
        elif xw >= 0.38 or iso >= 0.20:
            pts += 2.25
        elif iso >= 0.16 or xw >= 0.35:
            pts += 1.5
        elif iso >= 0.13:
            pts += 0.75
        else:
            pts += 0.25
        # Recent form (3)
        rb, rhr = scb["recent_barrels"], scb["recent_hr"]
        if rhr >= 2 or rb >= 4:
            pts += 3; reasons.append(f"Hot ({rhr} HR / {rb} barrels last 7d)")
        elif rb >= 2:
            pts += 2
        elif rb >= 1:
            pts += 1
        # HR luck / 'due' (2) — v2.6 pairing luck with skill: expected window
        # HR from barrels (league ~50-60% of barrels become HR) vs actual.
        # A bat barreling balls that haven't cashed yet is the sharpest
        # positive-regression play; one whose HR >> barrels is riding luck.
        due_hr = None
        n_barrels = scb.get("barrels")
        w_hr = scb.get("window_hr")
        if n_barrels is not None and w_hr is not None:
            hpb = lg.get("hr_per_barrel") or LEAGUE_DEFAULT["hr_per_barrel"]
            due_hr = round(n_barrels * hpb - w_hr, 1)
            if due_hr >= 2:
                pts += 2; reasons.append(
                    f"DUE: {n_barrels} barrels, only {w_hr} HR in window "
                    f"(+{due_hr} xHR) — positive regression")
            elif due_hr >= 1:
                pts += 1
            elif due_hr <= -2.5:
                reasons.append(f"HR overperformance vs barrels ({due_hr} xHR) — luck risk")
        return (round(max(0.0, min(40.0, pts)), 1), reasons, "statcast",
                (ap >= 0.19 or br >= 0.10), mix_fit, due_hr)

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
    return round(min(30.0, pts), 1), reasons, "proxy", False, None, None


def score_total_bases(h, scb, scp, bats, throws, pcause, hf, slot, weather, lg=None):
    """v2.7 — Total-Bases upside: 0-100. A companion market to HR.

    TB (1B+2B·2+3B·3+HR·4) is a MUCH higher-probability outcome than a HR, so
    the shape weighting differs from the HR board: the HR board leans on
    air-pull (a HR-specific signal); TB leans on **making dangerous contact
    often**. So we up-weight hard-hit / xwOBAcon / contact-frequency and
    down-weight pull-air. HR is a subset of TB, so barrel/ISO still matter — a
    2+ TB night usually means an extra-base hit.

    Components (Statcast path):
      Contact quality 28 (xwOBAcon + hard-hit + sweet-spot)
      Contact frequency 20 (contact% + Z-contact — avoid the whiff, bank singles/dbls)
      Power / XBH juice 18 (barrel + ISO + SLG)
      Matchup 14 (platoon + pitch-mix fit + weak-pitcher tailwind)
      Environment 12 (park + temp — TB cares about overall offense, not just carry)
      Lineup 8 (PAs — top of order sees more pitches to bank TB)

    Proxy path (no Statcast): AVG/SLG/ISO only, capped at 62.
    Returns (tb_score, reasons, tier).
    """
    lg = lg or {}
    iso = max(0.0, h["slg"] - h["avg"])
    reasons = []

    def env_pts():
        pts = 0.0
        if hf is None:
            pts += 5.0
        else:
            pts += (8 if hf >= 108 else 6.5 if hf >= 103 else 5 if hf >= 98
                    else 3.5 if hf >= 93 else 2)
        if weather:
            t = weather.get("temp_f")
            if t is not None:
                pts += 4 if t >= 85 else 3 if t >= 72 else 2 if t >= 60 else 0.5
        else:
            pts += 2
        return min(12.0, pts)

    def matchup_pts():
        pts = 6.0
        if bats and throws:
            if bats != throws and bats in ("L", "R"):
                pts += 2; reasons.append(f"Platoon edge ({bats} vs {throws}HP)")
            elif bats == throws:
                pts -= 1.5
            elif bats == "S":
                pts += 1
        # weak pitcher = TB tailwind for the whole lineup (pcause is 0-25)
        if pcause >= 18:
            pts += 4; reasons.append("Soft-tossing / hittable arm (TB tailwind)")
        elif pcause >= 14:
            pts += 2.5
        elif pcause >= 10:
            pts += 1
        mf, _ = pitch_mix_fit(scb, scp) if scb else (None, None)
        if mf is not None:
            pts += max(-2.0, min(2.0, (mf - 1.0) * 4.0))
        return max(0.0, min(14.0, pts)), (mf if scb else None)

    def slot_pts():
        if slot is None:
            return 4.0
        return 8.0 if slot <= 2 else 7.0 if slot <= 5 else 4.0

    if scb:
        hh = scb["hard_hit_rate"]
        xw = scb.get("xwobacon") or 0.0
        ss = scb.get("sweet_spot_rate") or 0.0
        br = scb["barrel_rate"]
        cr = scb.get("contact_rate") or 0.0
        zc = scb.get("z_contact_rate") or 0.0
        pts = 0.0
        # Contact quality (28)
        q = 0.0
        q += 11 if xw >= 0.42 else 8.5 if xw >= 0.38 else 6 if xw >= 0.34 else 3 if xw >= 0.30 else 1
        q += 10 if hh >= 0.50 else 8 if hh >= 0.45 else 6 if hh >= 0.40 else 3.5 if hh >= 0.35 else 1.5
        q += 7 if ss >= 0.38 else 5 if ss >= 0.34 else 3.5 if ss >= 0.30 else 1.5
        pts += min(28.0, q)
        if xw >= 0.40 or hh >= 0.48:
            reasons.append(f"Dangerous contact (hard-hit {hh*100:.0f}%, xwOBAcon .{int(xw*1000):03d})")
        # Contact frequency (20) — the TB-specific edge vs the HR board
        f = 0.0
        f += 12 if cr >= 0.80 else 9 if cr >= 0.76 else 6.5 if cr >= 0.72 else 4 if cr >= 0.68 else 1.5
        f += 8 if zc >= 0.88 else 6 if zc >= 0.84 else 4 if zc >= 0.80 else 1.5
        pts += min(20.0, f)
        if cr >= 0.80 and zc >= 0.86:
            reasons.append(f"High-contact bat (contact {cr*100:.0f}%, Z-contact {zc*100:.0f}%) — banks XBH")
        elif zc < 0.75:
            reasons.append(f"Whiff risk (Z-contact {zc*100:.0f}%) — TB volatility")
        # Power / XBH juice (18)
        pw = 0.0
        pw += 10 if br >= 0.13 else 8 if br >= 0.10 else 6 if br >= 0.075 else 3.5 if br >= 0.05 else 1.5
        pw += 8 if iso >= 0.25 else 6 if iso >= 0.20 else 4 if iso >= 0.16 else 2 if iso >= 0.13 else 0.5
        pts += min(18.0, pw)
        # Matchup (14) + Env (12) + Lineup (8)
        mp, _ = matchup_pts()
        pts += mp + env_pts() + slot_pts()
        # Recent form nudge (share the HR window signal)
        rb = scb.get("recent_barrels", 0)
        if rb >= 3:
            pts += 2; reasons.append(f"Hot ({rb} barrels last 7d)")
        elif rb >= 1:
            pts += 1
        tb = round(max(0.0, min(100.0, pts)), 1)
        src = "statcast"
    else:
        pts = 0.0
        hr_rate = h["hr"] / h["pa"] if h["pa"] else 0
        pts += 22 if h["avg"] >= 0.290 else 17 if h["avg"] >= 0.265 else 12 if h["avg"] >= 0.245 else 6
        pts += 20 if h["slg"] >= 0.52 else 15 if h["slg"] >= 0.46 else 10 if h["slg"] >= 0.41 else 4
        pts += 12 if iso >= 0.22 else 8 if iso >= 0.17 else 4 if iso >= 0.13 else 1
        mp, _ = matchup_pts()
        pts += mp + env_pts() + slot_pts()
        reasons.append("No Statcast shape — TB score capped (season-rate proxy)")
        tb = round(min(62.0, pts), 1)
        src = "proxy"

    tier = ("Elite TB" if tb >= 80 else "Strong TB" if tb >= 70 else "Solid TB"
            if tb >= 60 else "Dart" if tb >= 50 else "Pass")
    return tb, reasons, tier, src


def wind_hr_effect(park, weather):
    """v2.5 — directional wind for open-air parks. Returns (pts_adj, reasons, warnings).

    Uses the park's approximate home-plate→CF bearing and Open-Meteo wind
    direction (meteorological: direction wind blows FROM). The out-to-CF
    component is  -speed·cos(wind_from − bearing):  +ve = blowing out (carry),
    −ve = blowing in (knockdown). Cosine component means a ±20° bearing error
    barely moves the number. Capped at ±3 env points; never fires under 7 mph.
    """
    if not park or park.get("roof") != "open" or not weather:
        return 0.0, [], []
    spd = weather.get("wind_mph") or 0
    wdir = weather.get("wind_dir_deg")
    bearing = park.get("cf_bearing_deg")
    if wdir is None or bearing is None:
        return (0.0, [], [f"High wind {spd:.0f} mph (direction unknown)"]
                ) if spd >= 15 else (0.0, [], [])
    out = -spd * math.cos(math.radians(wdir - bearing))
    if out >= 12:
        return 3.0, [f"Wind blowing OUT ~{out:.0f} mph toward CF (approx bearing)"], []
    if out >= 7:
        return 1.5, [f"Wind out ~{out:.0f} mph"], []
    if out <= -12:
        return -3.0, [], [f"Wind blowing IN ~{-out:.0f} mph (HR knockdown)"]
    if out <= -7:
        return -1.5, [], [f"Wind in ~{-out:.0f} mph"]
    if spd >= 15:
        return 0.0, [], [f"Crosswind {spd:.0f} mph (neutral-ish)"]
    return 0.0, [], []


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
        # v2.5 — directional wind (out = boost, in = penalty), open parks only
        w_pts, w_reasons, w_warn = wind_hr_effect(park, weather)
        pts += w_pts
        reasons += w_reasons
        warnings += w_warn
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
            apply_shrinkage(scb, scp_map, scmeta.get("league"))
            print(f"  statcast: {scmeta['batters']} batters, {scmeta['pitchers']} pitchers "
                  f"({scmeta['start']}..{scmeta['end']}), shrinkage applied "
                  f"(lg barrel {scmeta.get('league', {}).get('barrel_rate', '?')})")
            # v2.8 — say out loud which feed produced the shape layer.
            print(f"  statcast source: {scmeta.get('source', 'savant')} "
                  f"(savant {scmeta.get('days_savant', '?')}d / "
                  f"mlbapi {scmeta.get('days_mlbapi', 0)}d)")
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
        s, grade, flags = score_pitcher(p, scp_map.get(pid), (scmeta or {}).get("league"))
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
        h40, h_reasons, shape_src, pull_fit, mix_fit, due_hr = score_hitter(
            h, scb_h, scp_map.get(op["id"]) if op else None, bats, throws,
            (scmeta or {}).get("league"))
        park = park_for(ctx["venue"], parks)
        e_pts, e_reasons, e_warn, hf = score_environment(park, weather.get(ctx["gamePk"]), bats)
        l_pts, l_reasons, l_warn = score_lineup(slot, confirmed)
        c_pts, c_reasons = score_confidence(h40, pcause)
        warnings = e_warn + l_warn

        # v2.7 — Total-Bases companion score (higher-probability market)
        tb_score, tb_reasons, tb_tier, tb_src = score_total_bases(
            h, scb_h, scp_map.get(op["id"]) if op else None, bats, throws,
            pcause, hf, slot, weather.get(ctx["gamePk"]),
            (scmeta or {}).get("league"))

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
                and scb_h["air_pull_rate"] >= 0.22 and hf >= 105:
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
            "tb_score": tb_score, "tb_tier": tb_tier, "tb_reasons": tb_reasons[:4],
            "shape_source": shape_src, "pull_fit": pull_fit,
            "mix_fit": mix_fit, "fit_tag": fit_tag, "value_play": value_play,
            "due_hr": due_hr,
            "pitcher_grade": pgrade,
            "breakdown": {"pitcher": pcause, "hitter": h40, "environment": e_pts,
                          "lineup": l_pts, "confidence": c_pts},
            "statcast": ({k: scb[h["id"]].get(k) for k in
                          ("air_pull_rate", "barrel_rate", "hard_hit_rate", "fb_rate",
                           "z_contact_rate", "sweet_spot_rate", "xwobacon", "ev90")}
                         if scb.get(h["id"]) else None),
            "reasons": reasons, "warnings": warnings, "role_notes": role_notes,
        })

    candidates.sort(key=lambda c: c["total"], reverse=True)
    # v2.7 — TB board: same candidate pool ranked by total-bases upside.
    tb_board = sorted(
        ({"id": c["id"], "name": c["name"], "team": c["team"], "opp": c["opp"],
          "opp_pitcher": c["opp_pitcher"], "venue": c["venue"], "gamePk": c["gamePk"],
          "slot": c["slot"], "confirmed": c["confirmed"], "bats": c["bats"],
          "tb_score": c["tb_score"], "tb_tier": c["tb_tier"],
          "tb_reasons": c["tb_reasons"], "shape_source": c["shape_source"],
          "hr_total": c["total"], "hr_role": c["role"]}
         for c in candidates),
        key=lambda x: x["tb_score"], reverse=True)
    causes = build_causes(games, team_ctx, pcards, weather, parks, candidates)
    structures = build_structures(candidates)
    audit = build_audit_template(candidates, causes)

    # v2.7 — explicit data-quality block so a silently-degraded slate can never
    # masquerade as a full one (the mid-2026 failure mode: Savant 403s → proxy
    # shape → "picks aren't hitting" while the JSON looked normal).
    confirmed_n = sum(1 for tid in lineups if lineups[tid])
    n_statcast_bats = sum(1 for c in candidates if c["shape_source"] == "statcast")
    dq_warnings = []
    mode = "statcast" if scb else "proxy"
    if not scb:
        dq_warnings.append("PROXY MODE — no Statcast shape (season-power only, "
                           "scores capped, no Locked Core / pitch-mix fit / DUE). "
                           "Do not trust shape-based picks.")
    if games and confirmed_n == 0:
        dq_warnings.append("NO CONFIRMED LINEUPS — every bat is using a neutral "
                           "slot default. Likely built before lineups posted; "
                           "re-run closer to first pitch.")
    elif games and confirmed_n < 2 * len(games):
        # confirmed_n counts TEAMS with a posted lineup; a slate of N games has
        # 2N teams. (Comparing teams to games is what produced the v2.7.0
        # int-vs-list crash and an off-by-2x message.)
        dq_warnings.append(f"Only {confirmed_n}/{2 * len(games)} lineups confirmed — "
                           "unconfirmed bats use a neutral slot default.")
    if scmeta and scmeta.get("days_failed", 0) > 0:
        dq_warnings.append(f"Statcast: {scmeta['days_failed']} day(s) failed to "
                           f"fetch in the {window}-day window (thinner sample).")
    # v2.8 — a board built off the MLB Stats API fallback is a REAL shape board,
    # not proxy: air-pull / hard-hit / zone-contact / fly-ball are all live. Note
    # the two things that differ so the operator isn't guessing.
    sc_source = (scmeta or {}).get("source", "savant" if scb else "none")
    if scb and sc_source in ("mlbapi", "mixed"):
        dq_warnings.append(
            f"Shape built from the MLB Stats API fallback ({scmeta.get('days_mlbapi', 0)}"
            f"/{window} days) because Baseball Savant was unreachable. All shape "
            "signals are live; xwOBAcon is unavailable (regressed to league prior, "
            "ISO backstops it) and barrels are recomputed from EV/LA.")
    data_quality = {
        "mode": mode,
        "source": sc_source,
        "days_savant": (scmeta or {}).get("days_savant", 0),
        "days_mlbapi": (scmeta or {}).get("days_mlbapi", 0),
        "trustworthy": bool(scb) and confirmed_n > 0,
        "statcast_batters": (scmeta or {}).get("batters", 0),
        "statcast_candidates": n_statcast_bats,
        "confirmed_lineups": confirmed_n,
        "games": len(games),
        "days_failed": (scmeta or {}).get("days_failed", 0),
        "warnings": dq_warnings,
    }

    return {
        "version": "2.8",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "slate_date": date, "games": len(games),
        "confirmed_lineups": confirmed_n,
        "statcast": bool(scb),
        "statcast_meta": scmeta,
        "data_quality": data_quality,
        "method": "v2.6: rank causes → choose capture → lock confirmed shape that "
                  "captures the cause. NEW: zone-damage slot (hard-hit% × Z-Contact% — "
                  "loud contact that actually meets hittable pitches), xwOBAcon contact "
                  "quality, and HR-luck pairing (barrels-vs-HR xHR gap → DUE bats and "
                  "HR-DUE arms). Also: hardened Savant fetch w/ cache validation, "
                  "directional wind, EB shrinkage, platoon-split shape, pitch-mix fit "
                  "+ TRAP flags. Weights P25/Shape40(11 air-pull/8 barrel/7 zone-dmg/"
                  "6 matchup/3 quality/3 recent/2 due)/Env15/Lineup12/Src8. "
                  "v2.8: dual-source shape — Baseball Savant with an MLB Stats API "
                  "play-by-play fallback, so a Savant outage no longer drops the "
                  "board to proxy. "
                  + ({"savant": "Statcast (Savant) shape layer active.",
                      "mlbapi": "Statcast shape layer active via the MLB Stats API "
                                "fallback (Savant unreachable) — xwOBAcon regressed "
                                "to league prior, barrels recomputed from EV/LA.",
                      "mixed": "Statcast shape layer active (mixed Savant + MLB Stats "
                               "API days)."}.get(sc_source, "Statcast shape layer active.")
                     if scb else
                     "PROXY shape mode (no Statcast) — scores capped, no pitch-mix fit."),
        "tiers": {"Elite Core": "85+", "Core": "78-84", "Satellite": "70-77",
                  "Longshot": "62-69", "Pass": "<62"},
        "roles": ["Locked Core", "Mini-Stack Bat", "Power Satellite", "Cause Satellite",
                  "Core", "Longshot", "Watchlist", "Pass"],
        "causes": causes, "candidates": candidates, "structures": structures,
        "tb_board": tb_board,
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


def _load_existing_latest():
    try:
        with open(os.path.join(DATA, "latest.json")) as f:
            return json.load(f)
    except Exception:
        return None


def _should_publish(new_out, force=False):
    """v2.7 anti-downgrade guard.

    The mid-2026 failure mode: a day Savant 403'd produced a proxy build that
    silently *replaced* a good Statcast board on the live site. Guard: never let
    a proxy build overwrite latest.json when the currently-published board is a
    Statcast board for the same-or-newer slate date. The dated snapshot is still
    written either way, so no data is lost — only the site's `latest.json` is
    protected. `--force` overrides.

    Returns (publish: bool, reason: str).
    """
    if force:
        return True, "forced"
    cur = _load_existing_latest()
    if not cur:
        return True, "no existing latest.json"
    new_is_proxy = not new_out.get("statcast")
    cur_is_statcast = bool(cur.get("statcast"))
    if new_is_proxy and cur_is_statcast and cur.get("slate_date", "") >= new_out.get("slate_date", ""):
        return False, (f"refusing to downgrade published Statcast board "
                       f"({cur.get('slate_date')}) with a PROXY build "
                       f"({new_out.get('slate_date')}); dated snapshot still saved. "
                       f"Use --force to override.")
    return True, "ok"


def main():
    date = dt.date.today().isoformat()
    window, use_sc, force = 21, True, "--force" in sys.argv
    if "--date" in sys.argv:
        date = sys.argv[sys.argv.index("--date") + 1]
    if "--window" in sys.argv:
        window = int(sys.argv[sys.argv.index("--window") + 1])
    if "--no-statcast" in sys.argv:
        use_sc = False
    print(f"Building HR slate v2.7 for {date} ...")
    out = build(date, window=window, use_statcast=use_sc)
    os.makedirs(DATA, exist_ok=True)
    # Always persist the dated snapshot (backtest record) ...
    with open(os.path.join(DATA, f"picks-{date}.json"), "w") as f:
        json.dump(out, f, indent=2)
    # ... but protect the live board from a silent downgrade.
    publish, reason = _should_publish(out, force=force)
    if publish:
        with open(os.path.join(DATA, "latest.json"), "w") as f:
            json.dump(out, f, indent=2)
    else:
        print(f"  ⚠ NOT publishing to latest.json: {reason}")
    dq = out.get("data_quality", {})
    if dq.get("warnings"):
        print("  ⚠ DATA QUALITY:")
        for w in dq["warnings"]:
            print(f"      - {w}")
    print(f"  games={out['games']} candidates={len(out['candidates'])} "
          f"confirmed_lineups={out['confirmed_lineups']} statcast={out['statcast']} "
          f"mode={dq.get('mode')} published={publish}")
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
