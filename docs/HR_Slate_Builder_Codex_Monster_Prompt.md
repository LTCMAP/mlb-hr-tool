# Monster Prompt: Build the Odds-Free MLB HR Slate Builder

You are acting as a senior full-stack engineer, data engineer, and baseball analytics developer. Build a local-first, odds-free MLB home run slate grading tool based on Cameron Ireland's HR Pairing System Master Guide v2.2. The goal is not to scrape sportsbooks or predict exact odds. The goal is to create a free local tool that identifies the best HR causes on a daily MLB slate, grades hitters for capturing those causes, and recommends card structures such as RR core legs, mini-stacks, same-game correlated straights, and longshot convergence groups.

## Core Philosophy

The tool must follow this workflow:

CAUSE → CAPTURE → COVER → BATS → CONFIDENCE → STRUCTURE

Do not build this as a generic projection tool. Do not simply rank famous hitters. The tool should explain every recommendation through the guide's framework:

1. What is the HR cause?
2. Is the cause strong enough to cover with more than one bat?
3. Which hitters best capture that cause?
4. Are the hitters lineup-confirmed and likely to play all 9 innings?
5. Does the final structure fit the cause map?

The model should create decision support, not guaranteed picks.

## Hard Requirements

- Run locally first.
- Use free or no-key data sources where possible.
- Do not use paid odds APIs.
- Do not scrape sportsbooks.
- Do not require user accounts.
- Do not bypass paywalls or gated dashboards.
- Cache all external data locally to reduce calls and make debugging easier.
- Make every score explainable.
- Every output must show the reason, not just the grade.
- Treat undocumented public endpoints as fragile and isolate them in adapter files.
- Build with clean modular code so each data source can be replaced later.

## Recommended Local Tech Stack

Use Python unless there is a strong reason not to.

Recommended stack:

- Python 3.11+
- pandas or polars for data manipulation
- pydantic for typed models
- requests or httpx for API calls
- duckdb or sqlite for local cache/storage
- streamlit for the local dashboard MVP
- typer for CLI commands
- pytest for tests
- python-dotenv for local configuration if needed

Do not overbuild authentication, cloud hosting, user management, or payment features. This is a local analytical dashboard.

## Suggested Repo Structure

Create this structure:

```text
mlb-hr-slate-builder/
  README.md
  pyproject.toml
  .gitignore
  .env.example
  docs/
    HR_Pairing_System_Master_Guide_v2.2_Amended.pdf
    data_sources.md
    scoring_model.md
    audit_protocol.md
  data/
    cache/
    manual_inputs/
    exports/
    static/
      venues.csv
      park_factors.csv
      team_ids.csv
  src/
    hr_builder/
      __init__.py
      config.py
      models.py
      scoring.py
      capture.py
      structures.py
      explanations.py
      audit.py
      utils.py
      data_sources/
        __init__.py
        mlb_schedule.py
        statcast.py
        weather.py
        park_factors.py
        lineups.py
        manual_notes.py
      pipelines/
        build_slate.py
        refresh_data.py
        postgame_audit.py
      ui/
        streamlit_app.py
  tests/
    test_scoring.py
    test_capture.py
    test_structures.py
    test_weather.py
    test_manual_notes.py
```

## Data Source Plan

Build source adapters. Each adapter should return normalized pydantic models. The rest of the app should not care where the data came from.

### 1. Slate / Games / Probable Pitchers

Create `data_sources/mlb_schedule.py`.

Goal:
- Pull today's MLB games.
- Identify away team, home team, game time, venue, game status, probable pitchers when available, and game ID.
- Refresh close to lock because probable pitchers change.

Implementation notes:
- Use MLB public schedule data if available.
- Keep the endpoint logic isolated because public MLB endpoints may not be formally documented.
- If the automated adapter fails, support manual CSV input for slate and probables.

Required output model:

```python
GameSlateItem(
    game_pk: str | None,
    date: date,
    start_time_utc: datetime,
    away_team: str,
    home_team: str,
    away_team_id: int | None,
    home_team_id: int | None,
    venue_name: str,
    venue_id: int | None,
    probable_away_pitcher: PlayerRef | None,
    probable_home_pitcher: PlayerRef | None,
    status: str,
)
```

### 2. Venue / Ballpark Static Data

Create `data/static/venues.csv`.

Fields:

```text
venue_id,venue_name,team,city,state,latitude,longitude,timezone,orientation_degrees,roof_type,left_field_ft,center_field_ft,right_field_ft,notes
```

Purpose:
- Weather calls need latitude/longitude.
- Wind must be translated into in/out/cross relative to ballpark orientation.
- Park effects need handedness and pull-side context.

If exact orientation is difficult, create a TODO and use a basic wind text interpretation for v1. Do not fake precision. Mark confidence as `unknown` if orientation is missing.

### 3. Weather

Create `data_sources/weather.py`.

Goal:
- Pull hourly weather for each outdoor venue near game time.
- Return temperature, wind speed, wind direction, precipitation probability, humidity, and weather confidence.
- Translate wind to HR impact: out, in, cross, neutral, dome/roof unknown.

Use a free weather provider such as Open-Meteo or NWS.

Weather scoring rules:

- Temperature:
  - 85°F+ = positive
  - 75–84°F = slight positive
  - 50–60°F = negative
  - below 50°F = strong negative
- Wind:
  - 10+ mph out = strong positive cause
  - 6–9 mph out = positive
  - 10+ mph in = veto/downgrade
  - crosswind = neutral/slight depending direction
- Rain risk:
  - High delay/postponement risk = warning, not automatic no-bet
  - If hitters may lose plate appearances, lower lineup/opportunity confidence
- Roof:
  - If dome/roof closed = neutralize outdoor weather
  - If roof unknown = warning

Required output model:

```python
WeatherSnapshot(
    venue_id: int | None,
    game_pk: str | None,
    game_time_local: datetime,
    temp_f: float | None,
    wind_speed_mph: float | None,
    wind_direction_deg: float | None,
    wind_effect: Literal['out','in','cross','neutral','unknown','roof'],
    precip_probability: float | None,
    humidity: float | None,
    roof_status: Literal['open','closed','dome','unknown'],
    weather_score: float,
    warnings: list[str],
)
```

### 4. Statcast / Baseball Savant Data

Create `data_sources/statcast.py`.

Goal:
- Pull or load Statcast data for rolling windows.
- Calculate hitter and pitcher features used in the guide.
- Cache data by date and window.

Windows:
- Season to date
- Last 30 days
- Last 14 days
- Last 7 or 10 days

Pitcher features:
- HR/9 or HR per BBE/PA proxy
- Barrel% allowed
- Hard-hit% allowed
- Fly-ball% allowed
- Ground-ball% allowed
- K rate
- Swinging strike proxy if available
- Pitch mix percentages
- Pitch locations by zone if feasible
- Fastball velocity and usage
- Recent command/contact quality trend
- Starter experience/leash proxy if available

Hitter features:
- Barrel rate
- Hard-hit rate
- Pull rate
- Air-pull rate or pulled air-ball proxy
- Fly-ball rate
- Launch angle distribution
- xwOBA / xSLG / ISO proxy where available
- Platoon split data if available
- Pitch-type performance by arsenal if feasible
- Recent form: HR, barrels, hard hits over 7–14 days
- Bat-tracking tiebreakers if available: fast swing rate, squared-up rate, blasts

Important metric philosophy:
- Air-pull and pulled-air profile should be central for HR betting.
- Barrels and blasts are power-quality confirmation.
- Bat tracking is a tiebreaker, not the main driver.
- Recent form is a multiplier, not an independent cause.
- Do not let batting average drive HR decisions.

### 5. Park Factors

Create `data_sources/park_factors.py`.

Goal:
- Load a static park-factor file or cached Baseball Savant export.
- Support handedness-specific HR and distance factors if available.
- Support day/night and roof/open-air when available.

Minimum v1 fields:

```text
venue_id,venue_name,bat_side,hr_factor,distance_factor,run_factor,source_years,notes
```

Park scoring:
- HR factor > 110 = strong positive
- 105–110 = slight positive
- 95–104 = neutral
- 90–94 = slight negative
- below 90 = negative

Add pull-side porch logic later if spray direction and wall dimensions are available.

### 6. Lineups

Create `data_sources/lineups.py`.

Goal:
- Determine whether lineups are confirmed or expected.
- Support manual lineup entry if automated data is unavailable.
- Penalize pinch-hit risk and low lineup slots.

Fields:

```python
LineupEntry(
    game_pk: str | None,
    team: str,
    player: PlayerRef,
    batting_order: int | None,
    position: str | None,
    bats: Literal['L','R','S','unknown'],
    confirmed: bool,
    source: str,
    pinch_hit_risk: Literal['low','medium','high','unknown'],
)
```

Opportunity scoring:
- Confirmed lineup = required for final locking.
- Batting 1–2 = strong opportunity bonus.
- Batting 3–4 = anchor opportunity.
- Batting 5–6 = acceptable, especially for cheap/platoon power.
- Batting 7–9 = downgrade unless environment is elite.
- High pinch-hit risk = major penalty.
- Not confirmed = hold/warning, not final recommendation.

### 7. Manual Notes Input: Kasper / Outlaw / User Reads

Create `data_sources/manual_notes.py`.

Goal:
- Allow the user to paste daily text notes like Kasper slate breakdown and Outlaw board.
- Parse names, teams, opponent pitchers, matchup notes, and source tags.
- Do not require perfect NLP. Build a simple structured manual editor after parsing.

Manual note fields:

```python
ManualSignal(
    player_name: str,
    team: str | None,
    opposing_pitcher: str | None,
    source: Literal['Kasper','Outlaw','User','Other'],
    signal_type: Literal['favorite','model_pick','zone_fit','KHR','barrel','form','longshot','avoid','unknown'],
    confidence: Literal['strong','medium','weak','unknown'],
    raw_note: str,
)
```

Parsing rules:
- Outlaw format often has game headers and player odds, but ignore odds in v1.
- Kasper notes often include opposing pitcher weakness and reasoning. Preserve the raw reason.
- If a player appears in both Kasper and Outlaw, give source convergence credit.
- If a deep/less obvious player appears from multiple sources, flag as “deep convergence.”
- Allow manual correction in UI.

## Core Data Models

Create `models.py` with pydantic models.

Essential models:

```python
PlayerRef
GameSlateItem
Venue
WeatherSnapshot
PitcherProfile
HitterProfile
LineupEntry
ManualSignal
PitcherCauseCard
HitterCandidateCard
CaptureRecommendation
StructureRecommendation
SlateBuildResult
AuditRecord
```

Every recommendation should include:
- score
- tier
- role
- reasons
- warnings
- data freshness
- source confidence

## Scoring Model

Create `scoring.py`.

Use a 100-point odds-free leg score:

1. Pitcher Cause — 30 points
2. Hitter HR Profile — 35 points
3. Environment — 15 points
4. Lineup / Opportunity — 10 points
5. Source Confidence / Structure Fit — 10 points

### Pitcher Cause — 30

Flags:
- HR/9 or HR-contact proxy elevated
- Barrel% allowed elevated
- Hard-hit% allowed elevated
- Fly-ball% elevated
- K rate low
- Fastball shape/velocity vulnerable or heavy predictable pitch usage
- Recent command/contact quality poor
- Rookie or short-track starter
- Long leash / likely third-time-through exposure
- Bad bullpen behind him, if bullpen data is available

Scoring:
- 0–9 = no clear pitcher cause
- 10–17 = weak/moderate cause
- 18–23 = solid fade target
- 24–30 = elite fade target

Pitcher cause grades:
- A+ = 24+ plus positive environment or bad bullpen
- A = 21–23
- B = 17–20
- C = 12–16
- Pass = below 12

### Hitter HR Profile — 35

Features:
- Air-pull / pulled-air profile
- Barrel rate
- Hard-hit rate
- Fly-ball rate
- Platoon ISO or power split
- Pitch-type fit versus pitcher arsenal
- Zone fit if available
- Recent HR/barrel form
- Bat-tracking tiebreakers: blasts, squared-up, fast swing rate

Scoring:
- 0–10 = weak HR shape
- 11–20 = playable but not special
- 21–27 = strong HR profile
- 28–35 = elite HR profile for this matchup

### Environment — 15

Features:
- Park HR factor by handedness
- Distance factor
- Pull-side porch fit if available
- Temperature
- Wind effect
- Roof status
- Rain/PA risk

Scoring:
- 0–4 = negative/veto environment
- 5–8 = neutral
- 9–12 = positive
- 13–15 = elite carry environment

### Lineup / Opportunity — 10

Features:
- Confirmed lineup
- Batting slot
- Platoon removal risk
- Plays all 9 likelihood
- Plate appearance expectation

Scoring:
- confirmed top 4 = high score
- confirmed 5–6 = medium/high
- confirmed 7–9 = lower unless strong cause
- unconfirmed = warning and cap score
- high pinch-hit risk = major penalty

### Source Confidence / Structure Fit — 10

Features:
- Kasper signal
- Outlaw signal
- User signal
- Statcast agreement
- Deep convergence
- Fits RR/straight/mini-stack role
- Not over-concentrating one game

Scoring:
- 0–3 = no external/manual convergence
- 4–6 = one good source or model-only support
- 7–8 = two-source agreement
- 9–10 = deep convergence or perfect role fit

## Score Tiers

Use these tier labels:

- 85+ = Elite Core
- 78–84 = Core
- 70–77 = Satellite / Mini-stack
- 62–69 = Longshot / Bonus-only
- Below 62 = Pass

Important: a high score does not automatically mean RR core. The role depends on cause map and capture method.

## Capture Layer

Create `capture.py`.

This is the key differentiator. After identifying a cause, choose the capture method before selecting final bats.

Capture methods:

1. Single Bat
2. 2-Bat Mini-Stack
3. Same-Game Correlated Pair
4. Cross-Stack
5. Team HR Market Placeholder
6. Pass

Since v1 has no odds and no team HR market API, show “Team HR Market Placeholder” only as a note when the cause is strong but bat capture is uncertain.

Cause strength table:

- A+ cause:
  - bad starter + bad bullpen or elite park/weather + multiple bats fit
  - recommend 2-bat mini-stack or team HR placeholder
- A cause:
  - starter has multiple fade flags and at least 2 bats fit
  - recommend 2-bat mini-stack if lineup confirms
- B cause:
  - one clear pitcher weakness and one clear bat fit
  - recommend single best bat or satellite
- C cause:
  - mostly hitter form/name value without real cause
  - watchlist or pass
- Public mega-cause:
  - many sources on same obvious spot
  - cap exposure and avoid anchoring alone

Rules:
- Strong cause with multiple playable bats should not be represented by only one hitter unless one hitter is clearly elite.
- RR legs should usually diversify causes.
- Straight tickets can concentrate correlation.
- Cap any single game/cause at roughly one-third of total suggested exposure.
- Do not pair two bats if the shared cause cannot be stated clearly.

## Structure Engine

Create `structures.py`.

The app should recommend structures, not place bets.

Structures:

1. 4-leg RR core by 2s
   - Goal: survival layer
   - Pick 4 hitters from diversified causes
   - Avoid loading 3+ hitters from same game

2. 3-leg straight
   - Goal: clean leverage
   - Can use one correlated same-game pair plus one strong standalone
   - Must explain correlation

3. Same-game 2-leg straight
   - Goal: pure same-cause correlation
   - Only when cause is strong and both bats fit

4. Longshot convergence group
   - Goal: weird value/watchlist group
   - Since there are no odds, classify by profile: lower lineup/name recognition but real cause and multi-source support

Ticket score:

For every recommended structure, calculate:

- Cause diversity: 0–3
- Correlation clarity: 0–3
- Confidence/data quality: 0–2
- Lineup/weather safety: 0–2

Total ticket score: 10.

RR should score high on cause diversity.
Straight should score high on correlation clarity.

## Dashboard Requirements

Create `ui/streamlit_app.py`.

Pages or sections:

### 1. Daily Setup

Inputs:
- Slate date
- Refresh data button
- Manual Kasper notes textbox
- Manual Outlaw notes textbox
- Manual lineup override upload
- Manual park/weather override upload, optional

Display:
- data freshness
- missing data warnings
- number of games loaded
- number of confirmed lineups

### 2. Slate Overview

Show all games as cards:
- teams
- venue
- probable pitchers
- weather summary
- total cause grade
- warnings
- recommended capture method

### 3. Pitcher Fade Board

Columns:
- pitcher
- opponent
- cause score
- fade flags
- weather/park boost
- bullpen note if available
- recommended cover method

### 4. Hitter Candidate Board

Columns:
- player
- team
- opponent pitcher
- batting slot
- confirmed?
- total score
- tier
- role
- top reasons
- warnings
- source tags

Filters:
- Core only
- Satellite only
- Longshots
- Confirmed lineups only
- Hide weather warnings
- By game
- By source convergence

### 5. Structure Builder

Show:
- suggested 4-leg RR core
- suggested same-game pair(s)
- suggested 3-leg straight(s)
- suggested longshot convergence group
- cause map explaining why each hitter belongs

Make it very clear that the tool is not using odds.

### 6. Export

Buttons:
- Export slate board to CSV
- Export hitter cards to CSV
- Export recommended structures to Markdown
- Export post-lock audit template

## CLI Requirements

Create Typer commands:

```bash
hr-builder refresh --date YYYY-MM-DD
hr-builder build --date YYYY-MM-DD
hr-builder serve
hr-builder export --date YYYY-MM-DD --format csv
hr-builder audit --date YYYY-MM-DD
```

## Audit System

Create `audit.py`.

The guide cares about process, not one-night results. Build an audit export.

For each candidate and final recommendation, track:
- player
- team
- game
- role
- score
- cause score
- capture method
- final result: HR yes/no
- did the pitcher/team cause fire?
- did a teammate homer instead?
- barrels by team if available
- HR allowed by target pitcher
- bullpen HR allowed
- lineup slot
- PA count
- weather result

Audit KPIs:
- cause-fire rate
- bat-capture rate given cause fired
- teammate-capture rate
- RR core hit rate
- same-game pair hit rate
- longshot group hit rate
- weather warning accuracy
- lineup confirmation accuracy

The key audit distinction:
- If the target pitcher/team environment produced HRs but the selected bat missed, that is a cause win but bat-capture miss.
- If the pitcher/environment did not produce HRs or barrels, that is a cause miss.

## Explanation Requirements

Create `explanations.py`.

Every card should include plain-English explanations.

Example hitter explanation:

“Gunnar Henderson grades as Core because the opponent pitcher has multiple HR fade flags, Gunnar owns the best left-handed power profile on the team, his air-pull/barrel shape fits the park, and he appears in both manual notes and model output. Downgrade if he is not confirmed top four in the lineup.”

Example capture explanation:

“This is an A cause with two playable bats, so the tool recommends a 2-bat mini-stack instead of forcing a single best hitter. Strong causes deserve coverage; weak causes deserve precision.”

Example structure explanation:

“This RR core uses four different causes, which fits the survival layer. The straight ticket uses a correlated pair because both bats share the same pitcher/weather cause.”

## Testing Requirements

Write tests for:

- Pitcher cause scoring with synthetic inputs
- Hitter HR scoring with synthetic inputs
- Weather scoring: wind out, wind in, dome, rain warning
- Capture method selection by cause grade
- RR core should avoid over-concentrating one game
- Same-game straight should require a clear shared cause
- Manual notes parser should identify player/source tags from pasted text
- Missing lineups should create warnings and cap confidence

## Acceptance Criteria

The MVP is complete when:

1. User can run locally with one command.
2. User can select today’s date and refresh the slate.
3. Tool loads games, probable pitchers, venues, weather, and cached/statcast-derived features.
4. User can paste Kasper and Outlaw notes.
5. Tool generates pitcher cause cards.
6. Tool generates hitter candidate cards with scores, tiers, roles, reasons, and warnings.
7. Tool recommends at least:
   - one diversified RR core
   - one same-game correlated pair if available
   - one longshot convergence group if available
8. Tool exports results to CSV/Markdown.
9. Tool creates an audit template for postgame review.
10. All recommendations are explainable and odds-free.

## Development Plan

Build in phases.

### Phase 1: Local Skeleton

- Create repo structure.
- Add models.
- Add scoring functions with synthetic/manual data.
- Add Streamlit dashboard using sample fixtures.
- Add export functions.

### Phase 2: Data Adapters

- Add MLB slate adapter.
- Add venue static file.
- Add weather adapter.
- Add Statcast/pybaseball adapter.
- Add cache layer.

### Phase 3: Manual Notes + Real Slate

- Add pasted notes parser.
- Add manual correction UI.
- Merge manual signals into candidate scoring.
- Generate real daily slate board.

### Phase 4: Structure + Audit

- Build RR core logic.
- Build same-game pair logic.
- Build longshot/convergence logic.
- Add audit export and postgame import.

### Phase 5: Calibration

- Store daily results.
- Track cause-fire and bat-capture rates.
- Tune weights monthly.
- Add calibration dashboard.

## Important Guardrails

- Do not make claims of guaranteed betting profit.
- Do not automate sportsbook betting.
- Do not include paid odds integrations in v1.
- Do not scrape sites that prohibit scraping.
- Do not make hidden scoring. The score must be transparent.
- Do not let manual public picks overpower the model. Manual picks are source-confidence signals, not automatic recommendations.
- Do not let recent form become a standalone cause.
- Do not allow unconfirmed lineups to appear as final locked plays without warnings.
- Do not over-concentrate RR structures in one game.
- Do not use batting average as a key HR metric.

## First Task

Start by creating the repo skeleton, pydantic models, scoring module, capture module, and a Streamlit dashboard that works with sample fixture data. Do not connect external data sources until the local scoring and UI are working.

Create sample fixture data for a fake 6-game slate with:
- one A+ cause
- two A causes
- two B causes
- one bad-weather warning
- confirmed and unconfirmed lineup examples
- manual source overlap examples

Then show the dashboard output and write tests for the scoring and capture decisions.

