# ⚾ Daily MLB Home Run Picks — odds-free (v2.8.1)

## What changed in v2.8.1 — the actual root cause

Savant was **never down**. `--diag` proved it: the bare `type=details` query
returns HTTP 200 with ~17 MB of real pitch-level data in ~34s.

Two things were wrong instead:

- **`group_by=name` silently poisons a details query.** Adding it (as the
  "canonical" Savant/pybaseball parameter set does) makes Savant return HTTP 200
  and ~3 MB of *player-aggregate* CSV — `"pitches","player_id","player_name",
  "total_pitches",…` — instead of pitch-level rows. It looks like a successful
  fetch and is useless. `group_by` is now gone from every variant, the bare
  query runs first, and `_looks_valid()` explicitly rejects an aggregate header
  so a wrong-shaped 200 can never be cached.
- **A cold 21-day window took ~12 minutes.** Each day is a ~17 MB download that
  Savant spends 30-40s building, and they ran one at a time — long enough that
  runs looked hung and got killed, which is what actually left the board in
  PROXY mode. Downloads now run **5 at a time** (~2-3 min cold, seconds warm),
  parse only after caching, so memory stays flat.

Also: `_game_pks()` checked only `codedGameState`, which statsapi doesn't always
populate — the fallback reported "0 completed games" on a date that had a full
slate. It now checks every status field, and `--diag` prints what it skipped.

## What changed in v2.8 — the shape layer stops depending on one feed

> **Note:** v2.8 was written on the assumption that Savant had stopped
> answering. It hadn't — see v2.8.1 above for the real root cause. The
> single-point-of-failure problem it fixed was still real, so the fallback
> stays; only the diagnosis was wrong.

Whatever the trigger, the shape layer had **one** source, and losing it dropped
the board to `PROXY` (season power only, scores capped, no Locked Core /
pitch-mix fit / DUE). v2.8 removes that dependency:

1. **Multiple query forms.** `statcast.py` tries the bare `type=details` query
   first, then longer parameter sets, and validates the *shape* of what comes
   back rather than trusting HTTP 200.
2. **A real second source.** If Savant is unreachable, the identical pitch-level
   rows are rebuilt from the **MLB Stats API play-by-play feed**
   (`statsapi.mlb.com/api/v1/game/{pk}/playByPlay`), which carries the same
   Statcast measurements: `launchSpeed`, `launchAngle`, `trajectory`, hit
   coordinates, pitch type, zone, bat side / throw hand. That is the feed
   `build.py` already uses for schedule and lineups — **if the slate builds at
   all, the shape layer can now build too.** No more PROXY on a Savant outage.
3. **Visible provenance.** `statcast_meta.source` is `savant` / `mlbapi` /
   `mixed`, and the dashboard banner says which. A degraded board can't look
   like a clean one.

**What the fallback costs you** (only while Savant is down):
- `xwOBAcon` isn't published by statsapi → it regresses to the league prior, and
  season ISO backstops the "dangerous contact" slot.
- Barrels are recomputed from EV/LA (98 mph @ 26-30°, widening ~1°/mph per side
  to 8-50° at 116+) rather than read from Savant's own classification.

Everything else — air-pull, barrel rate, hard-hit, fly-ball, Z-Contact,
sweet-spot, EV90, platoon splits, pitch-mix fit, HR-luck — is fully live.

Also in v2.8:
- **Fail fast.** The retry backoff no longer sleeps after the final attempt, and
  a circuit breaker stops re-probing Savant after 2 dead days in a run. A
  21-day window with Savant down went from **~10 minutes of dead sleep, then
  PROXY** to **~40 seconds and a real board**.
- **Zero-prior bug fixed.** A league block carrying an explicit `0.0` for a
  metric the source couldn't supply used to shrink every hitter toward `.000`
  instead of toward the league default. Missing metrics are now `None`.
- **`python3 statcast.py --diag`** — one screen telling you exactly which feed
  is failing, and whether it's a code problem or a network problem.

```
$ python3 statcast.py --diag
[1/3] Baseball Savant CSV endpoint   ...
[2/3] MLB Stats API fallback         ...
[3/3] Local cache                    ...
VERDICT: Savant is DOWN/blocked, but the MLB Stats API fallback works.
```

## What changed in v2.6
- **Savant fetch fixed + hardened.** Mid-2026 Baseball Savant started 403ing
  scripted user agents, which silently killed the Statcast layer. The fetch now
  sends browser-grade headers, handles gzip, retries with backoff, and —
  critically — **validates every response before caching** (a blocked page can
  no longer poison the cache as an "empty day"). Invalid cached files are
  auto-purged and refetched. The Actions cache key was bumped to drop the
  poisoned entries.
- **Zone-damage slot (7 pts):** hard-hit% **paired with Z-Contact%** (in-zone
  contact). Loud contact only cashes if the bat meets hittable pitches — the
  HH+Z-Ct combo tracks HR/FB far better than either metric alone.
- **Contact quality via xwOBAcon:** expected wOBA on contact (EV+LA) backstops
  season ISO, stripping park/defense noise out of "is this contact dangerous".
- **HR-luck pairing (skill vs. luck):** window xHR from barrels vs actual HR.
  Bats barreling without cashing get a **⇧ DUE** tag (+2); arms allowing loud
  contact without HR damage get **HR-DUE** (their HR/9 hides the fade); HR
  overperformers are flagged as luck risks.
- **New dashboard chips:** Z-contact, sweet-spot%, xwOBAcon, EV90.
- Shape 40 re-weighted: air-pull 11 / barrel 8 / zone-damage 7 / matchup 6 /
  quality 3 / recent 3 / due 2. New rates get their own EB-shrinkage priors.

A **free**, no-API-key tool that grades today's MLB hitters for home-run upside
and refreshes daily via a JSON file. Implements the **HR Pairing System** workflow:

> **CAUSE → CAPTURE → COVER → BATS → CONFIDENCE → STRUCTURE**

The clean v2.3 rule:

> **rank causes first → choose a capture method → lock confirmed hitters whose
> batted-ball shape (air-pull / barrel) can actually capture the cause.**
>
> Old leg: *good pitcher fade + good season power.*
> New leg: *good cause + correct capture + role fit + air-pull/barrel fit.*

Instead of predicting odds or scraping sportsbooks, it ranks the slate's **HR causes**
(vulnerable pitchers + friendly parks/weather), grades hitters who can **capture** each
cause using real **Statcast** shape, and suggests **card structures**. Every score is explainable.

## What changed in v2.3
- **Real Statcast layer** (Baseball Savant via `statcast.py`, stdlib — no `pybaseball`/`pip`):
  air-pull rate, barrel rate, hard-hit rate, fly-ball rate per hitter; barrel/fly-ball/HR
  allowed per pitcher. **No air-pull/barrel confirmation ⇒ hitter shape is capped** (season
  power can no longer masquerade as elite shape).
- **Re-weighted score:** Pitcher **25** / Hitter **shape 40** / Env **15** / Lineup **12** / Source **8**
  (shifted weight away from pitcher-cause toward actual HR swing shape).
- **Role eligibility gates** (a high score is *not* automatically a bet) + **hard caps** +
  an **elite-hitter override** lane (a great bat can beat a neutral spot).
- **Cause-first board** with a recommended **capture method** per cause, plus a *public heat* flag.
- **Suppressive parks** (HR factor ≤ 92) cap a bat's role unless it has pull-side/barrel fit.
- **Four-outcome audit template** (cause fired? our bat vs teammate?) for postgame review.

> Per direction, **unconfirmed lineups are not hard-gated** out of core — confirmed status is
> shown and batting slot still scores, but an unconfirmed bat can still surface.

## What's new in v2.4 — fit logic (right hitter inside the right game)
The cause scan was already finding the right *games*; v2.4 sharpens *which hitter* inside them.
- **Pitch-mix fit** — does this bat actually damage what *this* pitcher throws? Computed as
  `Σ pitcher_usage[family] × hitter_barrel_index[family]`, **relative to the hitter's own
  baseline** (so it measures matchup, not raw power — raw power is already in the barrel/
  air-pull slots). 1.0 = neutral, >1 punishes this arsenal, <1 poor fit. Folds into the shape
  score's matchup slot, so the within-game ranking actually reshuffles. Pitch families
  (fastball / breaking / offspeed) keep the 21-day samples usable.
- **TRAP tag** — a strong surface-shape bat whose fit vs *this* arsenal is poor (the famous-name,
  wrong-matchup trap). **PITCH-FIT tag** — shape that specifically punishes this pitcher.
- **★ VALUE tag** — off-chalk live dogs: real HR shape + cause *below* the core line (because the
  chalk bats don't all go on a given night). Filterable in the dashboard.
- **Barrel→HR park fit** — pull-air power × carry park surfaces as a tiebreaker reason.
- **Public-heat toned down** — flags only the single chalkiest A+/multi-bat spot, not every game.

## What's new in v2.5 — statistical hardening + wind

- **Directional wind is now modeled.** Each park carries an approximate home-plate→CF
  compass bearing (`cf_bearing_deg` in `parks.json`); Open-Meteo wind *direction* is
  resolved into an out/in component via cosine. Blowing out ≥12 mph = **+3 env**,
  blowing in ≥12 mph = **−3 env** (halved at 7–11 mph), open-air parks only. Wind can
  now *raise* a score, not just warn. Bearings are ±15–20° estimates, but the cosine
  component makes small errors negligible.
- **Empirical-Bayes shrinkage.** All 21-day Statcast rates (hitter barrel / air-pull /
  hard-hit / FB; pitcher barrel-allowed / FB-allowed / HR-per-PA) regress toward the
  league average *of the same window*, weighted by sample size (priors follow published
  stabilization research: ~40–70 BBE for contact-quality rates, ~350 PA for HR/PA).
  A hot 10-BBE week can no longer masquerade as elite shape — verified: a 29% raw
  barrel rate on <15 BBE reads ~11% after shrinkage.
- **Platoon-split shape.** Hitter barrel/air-pull is tracked separately **vs LHP and
  vs RHP** and blended (raw hand-split counts + 30-BBE overall prior) when the opposing
  hand is known. A bat whose barrels only come vs lefties now scores like it.
- **Air-pull fixed to true spray angle.** The old pull detector counted the entire pull
  *half* of the field (league "air-pull" read ~27%, so nearly everyone graded elite).
  Now uses the outer third by spray angle — league lands ~18%, matching Savant's
  published pulled-air rate — and thresholds are recalibrated (elite ≥28%).
- **Pitcher fly-ball rate fixed.** Previously counted line drives (league ~52%; every
  arm maxed the slot). Now true FB per BBE (league ~27%), thresholds recalibrated.

---

## What you get

- **`index.html`** — a self-contained dashboard (no build step, no framework):
  - Hitter Candidate Board with a 100-point score, tier, role, reasons & warnings
  - Pitcher Fade Board (cause grades A+/A/B/C)
  - Suggested structures (4-leg RR core, same-game correlated pair, longshot group)
  - Filters (Core+, confirmed lineups only, by game, hide warnings) + JSON export
- **`build.py`** — regenerates `data/latest.json` from free data (run by cron/Actions)
- **`data/parks.json`** — static park factors + coordinates
- **`.github/workflows/daily.yml`** — free daily auto-update on GitHub

## Data sources (all free, no key)

| Source | Used for | CORS |
|---|---|---|
| [MLB Stats API](https://statsapi.mlb.com) | schedule, probable pitchers, lineups, season stats, bat/throw hand — **and (v2.8) full Statcast shape via `playByPlay` when Savant is down** | ✅ |
| [Baseball Savant](https://baseballsavant.mlb.com) (Statcast) | air-pull, barrel, hard-hit, fly-ball; pitcher barrel/FB/HR allowed (primary source) | ❌ (server-side only) |
| [Open-Meteo](https://open-meteo.com) | hourly temperature / wind / precip near game time | ✅ |
| `data/parks.json` | HR park factors (handedness-aware), roof, lat/lon | local |

**Important:** Baseball Savant has **no browser CORS**, so the Statcast layer runs only in
`build.py` (Python/cron side). That makes the **daily JSON the high-quality, Statcast-enriched
source**. The in-browser "Build live" button is a clearly-labeled **proxy fallback** (season
ISO/HR-rate, capped — no Locked Core). For full v2.3 shape, run `build.py` (locally or via the
GitHub Action) and let the page read `data/latest.json`.

---

## Run it

### Option A — just open the dashboard (zero setup)
The page builds **live in the browser** when no cached JSON is present. Because it
fetches local files and live APIs, serve it over HTTP (browser security blocks
`fetch` from `file://`):

```bash
cd mlb-hr-tool
python3 serve.py            # http://localhost:8765
```

Open <http://localhost:8765>. Click **"↻ Build live (today)"** anytime to refetch
with the latest lineups.

### Option B — generate the daily JSON yourself
```bash
python3 build.py                 # today
python3 build.py --date 2026-06-23
```
Writes `data/latest.json` (what the site reads) and a dated `data/picks-YYYY-MM-DD.json`.

---

## Free daily auto-updates (GitHub)

1. Push this folder to a GitHub repo.
2. **Settings → Pages →** deploy from branch `main`, folder `/ (root)`. Your site is
   then live at `https://<you>.github.io/<repo>/`.
3. The included workflow (`.github/workflows/daily.yml`) runs **twice daily** (14:30
   & 19:30 UTC), regenerates `data/latest.json`, and commits it — so the site updates
   itself for free. Edit the `cron:` lines to change timing, or trigger manually from
   the **Actions** tab.

No servers, no keys, no cost.

---

## The 100-point leg score (v2.5)

| Component | Max | What it measures |
|---|---:|---|
| Pitcher Cause | 25 | HR/9 + Statcast HR/PA allowed, barrel-allowed, fly-ball-allowed, K rate |
| Hitter HR **Shape** | 40 | **air-pull 12 · barrel 9 · hard-hit 6 · matchup 6 (platoon + pitch-mix fit) · platoon ISO 4 · recent form 3** (Statcast). Proxy mode caps at 30. |
| Environment | 15 | Park HR factor (by handedness), temp, roof, **directional wind ±3**, rain warnings |
| Lineup / Opportunity | 12 | Batting slot (unconfirmed = neutral default, not gated) |
| Source / Structure fit | 8 | Model agreement |

**Score tiers:** 85+ Elite Core · 78–84 Core · 70–77 Satellite · 62–69 Longshot · <62 Pass.

**Roles (eligibility gates — a score is not a bet):**
`Locked Core` (shape ≥32, slot 1-5, A/A+ cause or elite override, clean park) ·
`Core` · `Mini-Stack Bat` · `Cause Satellite` (strong cause, decent bat) ·
`Power Satellite` (elite bat, weak cause — the override lane) · `Longshot` · `Watchlist` · `Pass`.

**Fit tags (v2.4, shown alongside the role):**
`PITCH-FIT` (shape punishes this arsenal) · `⚠ TRAP` (good surface shape, poor fit vs this arsenal) ·
`★ VALUE` (off-chalk live dog — real shape + cause below the core line).

**Hard caps:** shape <22 → max Longshot · suppressive park (≤92) + no pull/barrel fit → Watchlist ·
Pass-grade cause + non-elite bat → Pass.

---

## ⚠️ Honest limitations (read this)

- **Statcast is server-side only.** Real air-pull/barrel comes from Baseball Savant, which
  has no browser CORS — so it lives in `build.py`. The in-browser live build is a **capped
  proxy** (season ISO/HR-rate) and cannot produce a Locked Core. Run `build.py` for full shape.
- **Statcast sample sizes.** The window defaults to 21 days; hitters with <5 batted balls are
  skipped, but short windows are still noisy early. Tune with `--window N`.
- **Matchup fit (v2.4) uses pitch *families*, not exact pitch types.** Fit is barrel damage by
  fastball/breaking/offspeed vs the pitcher's family usage — enough sample over 21 days, but it
  doesn't model exact pitch types, zones, or pull-side porch geometry. It's a real fit signal,
  not a full plate-discipline model. The in-browser proxy build has **no** pitch-mix fit.
- **Wind bearings are approximate.** v2.5 models wind direction against each park's
  approximate CF bearing (±15–20°); the cosine component tolerates that error, but treat
  wind boosts as directional signal, not gospel. Capped at ±3 env points, ≥7 mph only.
- **No manual notes yet.** Source-confidence is model-only; pasting Kasper/Outlaw reads to boost
  convergence is the natural next feature.
- **Not betting advice.** Decision-support only. No guaranteed picks, no odds.

## Files
```
mlb-hr-tool/
  index.html              # the dashboard (open this)
  build.py                # daily JSON generator, v2.4 scoring (stdlib only)
  statcast.py             # Baseball Savant Statcast adapter (stdlib, cached)
  serve.py                # tiny local static server
  data/
    parks.json            # static park factors
    latest.json           # generated — what the site reads
    cache/statcast/       # cached daily Savant pulls (gitignored)
  .github/workflows/
    daily.yml             # free daily auto-update (caches Statcast)
  docs/                   # original HR Pairing System guide + prompt
```

### Tuning the Statcast window
```bash
python3 build.py --window 30          # 30-day shape window (bigger sample)
python3 build.py --no-statcast        # force proxy mode (fast, no Savant)
python3 statcast.py 2026-06-23 21     # inspect the raw window aggregates
```
