#!/usr/bin/env python3
"""
Backtest / results scorer for the HR + Total-Bases model (v2.7).

Why this exists
---------------
The model had no scoreboard. "Picks aren't hitting" was a gut feeling with
nothing measuring it, and the daily build silently fell back to *proxy* shape
whenever Baseball Savant blocked the pull — so a degraded slate looked identical
to a full one in the JSON. This tool turns "off" into numbers and, critically,
tells you WHICH kind of off:

  * a FEED problem  -> proxy-day picks hit far worse than Statcast-day picks;
  * a LOGIC problem -> tiers/roles aren't monotonic (an "85 Elite Core" doesn't
    actually homer more often than a "70 Satellite").

What it does
------------
1. Gathers every historical slate it can find:
     - committed dated snapshots  data/picks-YYYY-MM-DD.json
     - the git history of         data/latest.json   (one board per build; we
       keep the last/most-complete board per slate date). This backfills the
       record even though the Action only ever committed latest.json.
2. Fetches ACTUAL results per date from the free MLB Stats API (final games
   only): per-batter HR / total bases / hits, and per-pitcher HR allowed.
   Cached to data/cache/results/YYYY-MM-DD.json so re-runs are instant.
3. Grades: HR hit-rate and 2+/3+/4 TB rate by tier, role, score bucket, and
   **shape source (statcast vs proxy)**; a calibration curve; Locked-Core lift
   over the field; and cause-fired rate by pitcher grade.
4. Writes data/backtest.json (read by the dashboard) and prints a report.

Stdlib only. Network only for result fetching (MLB Stats API, no key).

  python3 backtest.py                 # last 30 slate-days it can find
  python3 backtest.py --days 60
  python3 backtest.py --start 2026-06-01 --end 2026-06-30
  python3 backtest.py --no-git        # only committed dated files
  python3 backtest.py --no-fetch      # grade only dates already cached (offline)
"""

import json
import os
import sys
import glob
import subprocess
import datetime as dt
from urllib.request import urlopen, Request

API = "https://statsapi.mlb.com/api/v1"
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
RESULTS_CACHE = os.path.join(DATA, "cache", "results")

# Roles we actually "bet" — the denominator for headline hit-rate.
ACTIONABLE = {"Locked Core", "Core", "Mini-Stack Bat", "Cause Satellite",
              "Power Satellite"}
SCORE_BUCKETS = [(90, 200, "90+"), (85, 90, "85-89"), (80, 85, "80-84"),
                 (75, 80, "75-79"), (70, 75, "70-74"), (65, 70, "65-69"),
                 (60, 65, "60-64"), (0, 60, "<60")]
TB_TIERS = ["Elite TB", "Strong TB", "Solid TB", "Dart", "Pass"]
TIERS = ["Elite Core", "Core", "Satellite", "Longshot", "Pass"]
ROLES = ["Locked Core", "Core", "Mini-Stack Bat", "Cause Satellite",
         "Power Satellite", "Longshot", "Watchlist", "Pass"]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def get(url):
    req = Request(url, headers={"User-Agent": "mlb-hr-tool-backtest/2.7"})
    with urlopen(req, timeout=45) as r:
        return json.load(r)


# --------------------------------------------------------------------------- #
# 1. Gather historical picks
# --------------------------------------------------------------------------- #
def _picks_from_dated_files():
    """Committed data/picks-YYYY-MM-DD.json snapshots -> {date: board}."""
    out = {}
    for path in glob.glob(os.path.join(DATA, "picks-*.json")):
        try:
            board = json.load(open(path))
            d = board.get("slate_date") or os.path.basename(path)[6:16]
            out[d] = board
        except Exception:
            continue
    return out


def _git_commits_for(relpath):
    try:
        r = subprocess.run(["git", "-C", ROOT, "log", "--format=%H",
                            "--", relpath], capture_output=True, text=True)
        return [c for c in r.stdout.split() if c]
    except Exception:
        return []


def _picks_from_git():
    """Reconstruct one board per slate date from the git history of latest.json.

    The daily Action only committed latest.json, so its git history is a de-facto
    archive of every board that was ever live. We keep, per slate date, the build
    with the most confirmed lineups (ties -> newest generated_utc): that's the
    most complete board the site actually served that day.
    """
    best = {}   # date -> (confirmed, generated, board)
    for commit in _git_commits_for("data/latest.json"):
        try:
            blob = subprocess.run(["git", "-C", ROOT, "show", f"{commit}:data/latest.json"],
                                  capture_output=True, text=True)
            if blob.returncode != 0 or not blob.stdout.strip():
                continue
            board = json.loads(blob.stdout)
        except Exception:
            continue
        d = board.get("slate_date")
        if not d:
            continue
        key = (board.get("confirmed_lineups", 0), board.get("generated_utc", ""))
        if d not in best or key > best[d][0]:
            best[d] = (key, board)
    return {d: v[1] for d, v in best.items()}


def collect_picks(use_git=True):
    """Merge sources; committed dated files win over git-reconstructed boards."""
    boards = _picks_from_git() if use_git else {}
    boards.update(_picks_from_dated_files())   # dated files override
    return boards


# --------------------------------------------------------------------------- #
# 2. Actual results (MLB Stats API, cached)
# --------------------------------------------------------------------------- #
def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def fetch_results_for_date(date):
    """Return {'final': bool, 'batters': {id:{hr,tb,hits,ab,doubles,triples}},
              'pitchers': {id:{hr_allowed,bf,outs}}} for one slate date.

    Uses the boxscore of every FINAL game. Doubleheaders aggregate per player.
    """
    sched = get(f"{API}/schedule?sportId=1&date={date}&hydrate=linescore")
    game_pks, any_live = [], False
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            st = g.get("status", {}).get("abstractGameState")
            if st == "Final":
                game_pks.append(g["gamePk"])
            elif st in ("Live", "Preview"):
                any_live = True
    batters, pitchers = {}, {}
    for pk in game_pks:
        try:
            box = get(f"{API}/game/{pk}/boxscore")
        except Exception:
            continue
        for side in ("home", "away"):
            team = box.get("teams", {}).get(side, {})
            for _, pdata in team.get("players", {}).items():
                pid = pdata.get("person", {}).get("id")
                if pid is None:
                    continue
                bat = pdata.get("stats", {}).get("batting", {})
                if bat:
                    b = batters.setdefault(pid, dict(hr=0, tb=0, hits=0, ab=0,
                                                     doubles=0, triples=0, pa=0))
                    b["hr"] += _int(bat.get("homeRuns"))
                    b["tb"] += _int(bat.get("totalBases"))
                    b["hits"] += _int(bat.get("hits"))
                    b["ab"] += _int(bat.get("atBats"))
                    b["doubles"] += _int(bat.get("doubles"))
                    b["triples"] += _int(bat.get("triples"))
                    b["pa"] += _int(bat.get("plateAppearances"))
                pit = pdata.get("stats", {}).get("pitching", {})
                if pit:
                    p = pitchers.setdefault(pid, dict(hr_allowed=0, bf=0, outs=0))
                    p["hr_allowed"] += _int(pit.get("homeRuns"))
                    p["bf"] += _int(pit.get("battersFaced"))
                    p["outs"] += _int(pit.get("outs"))
    return {"final": bool(game_pks) and not any_live,
            "games_final": len(game_pks), "any_live": any_live,
            "batters": {str(k): v for k, v in batters.items()},
            "pitchers": {str(k): v for k, v in pitchers.items()}}


def load_results(date, allow_fetch=True):
    """Cached results for a date; only caches once all games are Final."""
    os.makedirs(RESULTS_CACHE, exist_ok=True)
    path = os.path.join(RESULTS_CACHE, f"{date}.json")
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            pass
    if not allow_fetch:
        return None
    try:
        res = fetch_results_for_date(date)
    except Exception as e:
        print(f"  [results] {date}: fetch failed ({e})")
        return None
    if res["games_final"] and not res["any_live"]:
        json.dump(res, open(path, "w"))    # cache only settled days
    return res


# --------------------------------------------------------------------------- #
# 3. Grade (pure — no network; unit-testable)
# --------------------------------------------------------------------------- #
def _bucket(rows):
    """rows: list of dicts w/ played(bool), hr(int), tb(int). -> summary."""
    played = [r for r in rows if r["played"]]
    n_played = len(played)
    hr = sum(1 for r in played if r["hr"] >= 1)
    tb2 = sum(1 for r in played if r["tb"] >= 2)
    tb3 = sum(1 for r in played if r["tb"] >= 3)
    tb4 = sum(1 for r in played if r["tb"] >= 4)
    rate = lambda n: round(n / n_played, 4) if n_played else None
    return {"n": len(rows), "played": n_played, "benched": len(rows) - n_played,
            "hr": hr, "hr_rate": rate(hr),
            "tb2": tb2, "tb2_rate": rate(tb2),
            "tb3": tb3, "tb3_rate": rate(tb3),
            "tb4": tb4, "tb4_rate": rate(tb4)}


def grade(boards, results):
    """boards: {date: board}, results: {date: results-dict}. Pure aggregation."""
    graded_dates, rows, tb_rows, pitcher_rows = [], [], [], []
    per_date = []
    for date in sorted(boards):
        res = results.get(date)
        if not res or not res.get("final"):
            continue
        graded_dates.append(date)
        bres = res["batters"]
        board = boards[date]
        statcast_day = bool(board.get("statcast"))
        drows = []
        for c in board.get("candidates", []):
            pid = str(c["id"])
            r = bres.get(pid)
            played = bool(r) and (r["ab"] > 0 or r["pa"] > 0)
            row = {"date": date, "id": pid, "name": c["name"],
                   "total": c["total"], "tier": c["tier"], "role": c["role"],
                   "pitcher_grade": c.get("pitcher_grade", "?"),
                   "shape_source": c.get("shape_source", "proxy"),
                   "statcast_day": statcast_day,
                   "played": played,
                   "hr": r["hr"] if r else 0, "tb": r["tb"] if r else 0}
            rows.append(row)
            drows.append(row)
        # TB board (new boards only)
        for t in board.get("tb_board", []):
            pid = str(t["id"])
            r = bres.get(pid)
            played = bool(r) and (r["ab"] > 0 or r["pa"] > 0)
            tb_rows.append({"date": date, "id": pid, "tb_tier": t["tb_tier"],
                            "tb_score": t["tb_score"],
                            "shape_source": t.get("shape_source", "proxy"),
                            "played": played,
                            "hr": r["hr"] if r else 0, "tb": r["tb"] if r else 0})
        # cause-fired: did the targeted pitcher allow a HR?
        pres = res["pitchers"]
        for p in board.get("pitcher_board", []):
            pr = pres.get(str(p["id"]))
            if not pr or pr["bf"] == 0:
                continue
            pitcher_rows.append({"grade": p.get("grade", "?"),
                                 "fired": pr["hr_allowed"] >= 1,
                                 "hr_allowed": pr["hr_allowed"]})
        # per-date rollup (actionable roles only)
        act = [r for r in drows if r["role"] in ACTIONABLE]
        s = _bucket(act)
        per_date.append({"date": date, "statcast": statcast_day,
                         "actionable_picks": s["n"], "played": s["played"],
                         "hr": s["hr"], "hr_rate": s["hr_rate"],
                         "tb2_rate": s["tb2_rate"]})

    def by(keyfn, keys, subset=None):
        pool = rows if subset is None else [r for r in rows if subset(r)]
        out = {}
        for k in keys:
            out[k] = _bucket([r for r in pool if keyfn(r) == k])
        return out

    actionable_rows = [r for r in rows if r["role"] in ACTIONABLE]
    overall = _bucket(actionable_rows)
    baseline = _bucket(rows)           # whole 250-pool = slate baseline
    locked = _bucket([r for r in rows if r["role"] == "Locked Core"])

    # calibration on the full candidate pool, by score bucket
    calibration = []
    for lo, hi, label in SCORE_BUCKETS:
        s = _bucket([r for r in rows if lo <= r["total"] < hi])
        if s["played"]:
            calibration.append({"bucket": label, **s})

    # feed diagnostic: statcast-day vs proxy-day actionable picks
    feed = {
        "statcast_day": _bucket([r for r in actionable_rows if r["statcast_day"]]),
        "proxy_day": _bucket([r for r in actionable_rows if not r["statcast_day"]]),
    }

    # cause-fired by pitcher grade
    cause = {}
    for g in ("A+", "A", "B", "C", "Pass"):
        gp = [p for p in pitcher_rows if p["grade"] == g]
        if gp:
            cause[g] = {"n": len(gp), "fired": sum(1 for p in gp if p["fired"]),
                        "fire_rate": round(sum(1 for p in gp if p["fired"]) / len(gp), 4)}

    # TB board calibration
    tb_by_tier = {}
    for tier in TB_TIERS:
        tr = [r for r in tb_rows if r["tb_tier"] == tier]
        if tr:
            tb_by_tier[tier] = _bucket(tr)

    lc_lift = (round(locked["hr_rate"] / baseline["hr_rate"], 2)
               if locked["hr_rate"] and baseline["hr_rate"] else None)

    return {
        "dates_graded": graded_dates,
        "n_dates": len(graded_dates),
        "overall_actionable": overall,
        "slate_baseline": baseline,
        "locked_core": locked,
        "locked_core_lift_vs_baseline": lc_lift,
        "by_tier": by(lambda r: r["tier"], TIERS),
        "by_role": by(lambda r: r["role"], ROLES),
        "by_pitcher_grade": by(lambda r: r["pitcher_grade"], ["A+", "A", "B", "C", "Pass"]),
        "calibration": calibration,
        "feed_diagnostic": feed,
        "cause_fired_by_grade": cause,
        "tb_by_tier": tb_by_tier,
        "per_date": per_date,
        "verdict": _verdict(overall, baseline, locked, calibration, feed, tb_by_tier),
    }


def _verdict(overall, baseline, locked, calibration, feed, tb_by_tier):
    """Plain-language read of what the numbers say."""
    notes = []
    sd, pd = feed["statcast_day"], feed["proxy_day"]
    if sd["played"] >= 20 and pd["played"] >= 20 and sd["hr_rate"] and pd["hr_rate"]:
        if sd["hr_rate"] >= pd["hr_rate"] * 1.3:
            notes.append(
                f"FEED is a real driver: Statcast-day picks homer {sd['hr_rate']*100:.1f}% "
                f"vs {pd['hr_rate']*100:.1f}% on proxy days "
                f"({sd['hr_rate']/pd['hr_rate']:.1f}x). Keep the Savant feed alive; "
                "proxy slates are materially worse.")
        else:
            notes.append(
                f"Feed gap is small (Statcast {sd['hr_rate']*100:.1f}% vs proxy "
                f"{pd['hr_rate']*100:.1f}%) — the misses aren't mostly the feed.")
    # calibration monotonicity across the top buckets
    rated = [c for c in calibration if c["played"] >= 10 and c["hr_rate"] is not None]
    if len(rated) >= 3:
        top = rated[0]["hr_rate"]
        bot = rated[-1]["hr_rate"]
        if top <= bot:
            notes.append(
                "CALIBRATION is inverted at the top: the highest-score bucket is NOT "
                "hitting more HR than lower buckets — the scoring weights need a look.")
        elif top < bot * 1.2:
            notes.append(
                "CALIBRATION is flat: top scores barely beat low scores on HR rate. "
                "The score is sorting weakly.")
        else:
            notes.append("Calibration is positive: higher scores do homer more often.")
    if locked["played"] and baseline["hr_rate"] and locked["hr_rate"] is not None:
        if locked["hr_rate"] <= baseline["hr_rate"]:
            notes.append(
                f"Locked Core ({locked['hr_rate']*100:.1f}%) is NOT beating the slate "
                f"baseline ({baseline['hr_rate']*100:.1f}%) — the flagship role isn't earning it.")
    if not notes:
        notes.append("Not enough settled dates yet to draw a confident verdict — "
                     "let more slates accumulate.")
    return notes


# --------------------------------------------------------------------------- #
# 4. Report
# --------------------------------------------------------------------------- #
def _pct(x):
    return "  –  " if x is None else f"{x*100:5.1f}%"


def print_report(rep):
    print("\n" + "=" * 64)
    print(f"  BACKTEST — {rep['n_dates']} settled slate-day(s)")
    if rep["dates_graded"]:
        print(f"  {rep['dates_graded'][0]} .. {rep['dates_graded'][-1]}")
    print("=" * 64)
    o, b, lc = rep["overall_actionable"], rep["slate_baseline"], rep["locked_core"]
    print(f"\n  Actionable picks : {o['played']:>4} played  |  HR {_pct(o['hr_rate'])}"
          f"  |  2+TB {_pct(o['tb2_rate'])}")
    print(f"  Slate baseline   : {b['played']:>4} played  |  HR {_pct(b['hr_rate'])}"
          f"  |  2+TB {_pct(b['tb2_rate'])}")
    print(f"  Locked Core      : {lc['played']:>4} played  |  HR {_pct(lc['hr_rate'])}"
          f"  |  lift x{rep['locked_core_lift_vs_baseline']}")

    print("\n  HR hit-rate by TIER")
    for t in TIERS:
        s = rep["by_tier"].get(t, {})
        if s.get("played"):
            print(f"    {t:<12} {s['played']:>4} played   HR {_pct(s['hr_rate'])}   2+TB {_pct(s['tb2_rate'])}")

    print("\n  Calibration (HR hit-rate by score bucket — should DECREASE down the list)")
    for c in rep["calibration"]:
        bar = "#" * int((c["hr_rate"] or 0) * 100)
        print(f"    {c['bucket']:<7} {c['played']:>4} played   HR {_pct(c['hr_rate'])}  {bar}")

    fd = rep["feed_diagnostic"]
    print("\n  FEED DIAGNOSTIC (actionable picks)")
    print(f"    Statcast days  {fd['statcast_day']['played']:>4} played   HR {_pct(fd['statcast_day']['hr_rate'])}")
    print(f"    Proxy days     {fd['proxy_day']['played']:>4} played   HR {_pct(fd['proxy_day']['hr_rate'])}")

    if rep["tb_by_tier"]:
        print("\n  TOTAL BASES board — 2+TB hit-rate by tier")
        for t in TB_TIERS:
            s = rep["tb_by_tier"].get(t, {})
            if s.get("played"):
                print(f"    {t:<10} {s['played']:>4} played   2+TB {_pct(s['tb2_rate'])}   3+TB {_pct(s['tb3_rate'])}")

    if rep["cause_fired_by_grade"]:
        print("\n  CAUSE FIRED (targeted pitcher allowed >=1 HR) by grade")
        for g in ("A+", "A", "B", "C", "Pass"):
            s = rep["cause_fired_by_grade"].get(g)
            if s:
                print(f"    {g:<3} {s['n']:>4} arms   fired {_pct(s['fire_rate'])}")

    print("\n  VERDICT")
    for v in rep["verdict"]:
        print(f"    • {v}")
    print()


# --------------------------------------------------------------------------- #
def main():
    args = sys.argv[1:]
    days = 30
    start = end = None
    use_git = "--no-git" not in args
    allow_fetch = "--no-fetch" not in args
    if "--days" in args:
        days = int(args[args.index("--days") + 1])
    if "--start" in args:
        start = args[args.index("--start") + 1]
    if "--end" in args:
        end = args[args.index("--end") + 1]

    boards = collect_picks(use_git=use_git)
    if not boards:
        print("No historical picks found (no dated files, no git history).")
        return
    dates = sorted(boards)
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    # don't grade today or the future (games not settled)
    today = dt.date.today().isoformat()
    dates = [d for d in dates if d < today][-days:]
    boards = {d: boards[d] for d in dates}
    print(f"Found {len(dates)} slate-day board(s) to grade: {dates[0]} .. {dates[-1]}"
          if dates else "No past slate dates to grade.")

    results = {}
    for d in dates:
        r = load_results(d, allow_fetch=allow_fetch)
        if r and r.get("final"):
            results[d] = r
            fin = "cached" if os.path.exists(os.path.join(RESULTS_CACHE, f"{d}.json")) else "live"
            print(f"  {d}: {r['games_final']} final games ({fin})")
        else:
            print(f"  {d}: no settled results (skipped)")

    rep = grade(boards, results)
    with open(os.path.join(DATA, "backtest.json"), "w") as f:
        json.dump({"generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                   **rep}, f, indent=2)
    print_report(rep)
    print(f"  wrote data/backtest.json")


if __name__ == "__main__":
    main()
