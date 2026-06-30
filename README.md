# ⚾ Daily MLB Home Run Picks — odds-free (v2.3)

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
| [MLB Stats API](https://statsapi.mlb.com) | schedule, probable pitchers, lineups, season stats, bat/throw hand | ✅ |
| [Baseball Savant](https://baseballsavant.mlb.com) (Statcast) | air-pull, barrel, hard-hit, fly-ball; pitcher barrel/FB/HR allowed | ❌ (server-side only) |
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

## The 100-point leg score (v2.3)

| Component | Max | What it measures |
|---|---:|---|
| Pitcher Cause | 25 | HR/9 + Statcast HR/PA allowed, barrel-allowed, fly-ball-allowed, K rate |
| Hitter HR **Shape** | 40 | **air-pull 12 · barrel 9 · hard-hit 6 · matchup/synergy 6 · platoon ISO 4 · recent form 3** (Statcast). Proxy mode caps at 30. |
| Environment | 15 | Park HR factor (by handedness), temp, roof, rain/wind warnings |
| Lineup / Opportunity | 12 | Batting slot (unconfirmed = neutral default, not gated) |
| Source / Structure fit | 8 | Model agreement |

**Score tiers:** 85+ Elite Core · 78–84 Core · 70–77 Satellite · 62–69 Longshot · <62 Pass.

**Roles (eligibility gates — a score is not a bet):**
`Locked Core` (shape ≥32, slot 1-5, A/A+ cause or elite override, clean park) ·
`Core` · `Mini-Stack Bat` · `Cause Satellite` (strong cause, decent bat) ·
`Power Satellite` (elite bat, weak cause — the override lane) · `Longshot` · `Watchlist` · `Pass`.

**Hard caps:** shape <22 → max Longshot · suppressive park (≤92) + no pull/barrel fit → Watchlist ·
Pass-grade cause + non-elite bat → Pass.

---

## ⚠️ Honest limitations (read this)

- **Statcast is server-side only.** Real air-pull/barrel comes from Baseball Savant, which
  has no browser CORS — so it lives in `build.py`. The in-browser live build is a **capped
  proxy** (season ISO/HR-rate) and cannot produce a Locked Core. Run `build.py` for full shape.
- **Statcast sample sizes.** The window defaults to 21 days; hitters with <5 batted balls are
  skipped, but short windows are still noisy early. Tune with `--window N`.
- **Matchup fit is partial.** True pitch-type-vs-arsenal fit needs per-pitch joins; v2.3
  approximates it with platoon hand + an air-pull/fly-ball *synergy* bonus. Pull-side porch
  geometry isn't modeled per park.
- **Wind direction is not modeled.** Without per-park orientation, wind only raises a *warning*
  at high speeds; it never inflates a score.
- **No manual notes yet.** Source-confidence is model-only; pasting Kasper/Outlaw reads to boost
  convergence is the natural next feature.
- **Not betting advice.** Decision-support only. No guaranteed picks, no odds.

## Files
```
mlb-hr-tool/
  index.html              # the dashboard (open this)
  build.py                # daily JSON generator, v2.3 scoring (stdlib only)
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
