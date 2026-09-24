# Edge Platform — automated odds monitor

## Phase 2

This build adds an unattended monitoring layer around the existing odds engine.

### Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# put provider keys in the environment / .env
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000/`.

### Automated scanner

The scanner is **OFF by default**. Turn it on from the dashboard or set:

```text
AUTO_SCANNER=1
```

Important settings:

- `SCANNER_INTERVAL_SECONDS` — minimum 60 seconds
- `SCANNER_SPORTS` — comma-separated supported sport keys
- `SCANNER_MIN_EV`
- `SCANNER_MIN_BOOKS`
- `SCANNER_MAX_ODDS_AGE`

The scheduler stores every odds snapshot, scan run, qualifying opportunity and alert.

The scanner does **not** place real-money bets.

### UK bookmaker coverage

The Odds API request uses `regions=uk,eu`, so the scanner can compare every UK-facing bookmaker returned by the connected feed. This is **not a guarantee of every UK bookmaker in existence**. Coverage is provider-dependent and the dashboard reports the bookmaker feeds actually observed.

### API

Useful endpoints:

- `GET /scanner/status`
- `POST /scanner/config`
- `POST /scanner/scan`
- `GET /scanner/opportunities`
- `GET /scanner/alerts`

### Security

Do not put provider API keys into the GitHub Pages frontend for production. The backend should own provider credentials. Restrict `ALLOWED_ORIGINS` to your actual frontend origin before public deployment and add authentication/rate limiting before exposing the backend to the public internet.

### Research warning

The automated scanner finds market/model discrepancies. It does not establish a profitable betting edge. CLV, calibration and genuinely out-of-sample testing remain the evidence required to support such a claim.

## Phase 3 — research / forecasting engine

This build adds a separate research layer rather than silently treating market consensus as a predictive model.

### What is now implemented

- **Provider-independent event IDs** based on normalized teams + kickoff minute.
- **Settled-result store** for football outcomes.
- **API-Football result ingestion** by league/date via `POST /research/ingest-results`.
- **Dynamic Elo** with home advantage and explicit draw probability.
- **Regularized Poisson goal model with a fixed Dixon-Coles low-score correction** fitted to settled results.
- **Model-only ensemble calibration**: Elo/DC blend is selected on a chronological validation slice using Brier score; bookmaker prices are not used to choose the model weight.
- **Strict walk-forward backtesting**: each prediction is generated using only results before that match.
- **Model-vs-market evaluation**: historical odds are matched using normalized teams and kickoff time, so provider-specific event IDs do not have to match.
- **Research model registry** and prediction log for reproducibility.
- Dashboard **Research** tab for status, fitting and walk-forward backtests.

### Research endpoints

- `GET /research/status?sport=soccer_epl`
- `POST /research/ingest-results?sport=soccer_epl&date=YYYY-MM-DD&season=2025`
- `POST /research/fit?sport=soccer_epl`
- `POST /research/backtest?sport=soccer_epl&min_training=30&retrain_every=25`
- `GET /research/predict/{event_id}?sport=soccer_epl`
- `POST /research/outcomes?sport=soccer_epl` for importing a list of settled results directly

Example result payload for `/research/outcomes`:

```json
[
  {"kickoff":"2026-09-20T14:00:00Z","home":"Arsenal","away":"Chelsea","home_goals":2,"away_goals":1,"event_id":"optional-provider-id"}
]
```

### Building a meaningful historical dataset

For serious testing, ingest **months/seasons of settled results and timestamped odds snapshots** before interpreting model performance. A few dozen matches are enough to exercise the pipeline, not enough to establish predictive superiority. The scanner's live snapshots can accumulate forward-looking odds history; API-Football can supply settled results where the plan/key permits it.

The walk-forward report exposes Brier score, log loss and qualifying model EV observations. Those metrics should be read alongside CLV and sample size. No profitability claim is made by the software.

## Phase 4 — validation, calibration and performance diagnostics

The research layer now also includes:

- **Chronological temperature calibration** for multiclass probabilities.
- **Expected calibration error (ECE)** and maximum calibration gap diagnostics.
- **Bookmaker probability diagnostics** using each bookmaker's own pre-kickoff h2h prices after de-vigging. These are diagnostics, not a bookmaker ranking.
- **Walk-forward v2** with flat-stake P/L, ROI, maximum drawdown and CLV diagnostics.
- **Bootstrap 95% intervals** for Brier, log loss, ROI and CLV where enough observations exist.
- **Independent latest-snapshot matching per bookmaker**, so feeds polled seconds apart are not incorrectly discarded because their timestamps differ.
- Dashboard controls for calibration and bookmaker diagnostics.

New endpoints:

- `POST /research/calibrate?sport=soccer_epl&min_training=60`
- `GET /research/bookmakers?sport=soccer_epl&min_observations=30`
- `POST /research/backtest-v2`

The backtest remains deliberately conservative: it does not assume successful execution, unlimited stakes, zero slippage, or guaranteed availability. Positive historical ROI alone is not treated as proof of a durable edge; CLV, calibration, sample size and out-of-sample replication matter.

## Phase 5 — durable historical data pipeline

Phase 5 adds a separate timestamped odds collector and research-data layer. The live collector can run independently of the scanner and stores every captured snapshot in SQLite. It also creates canonical event links so provider-specific event IDs can be reconciled.

Endpoints:
- `GET /data/status` — collector and dataset coverage
- `POST /data/config` — enable/disable scheduled capture
- `POST /data/capture` — capture a snapshot immediately
- `POST /data/results` — import settled API-Football results for a season
- `POST /data/historical-odds` — import historical The Odds API snapshots when the account has historical access
- `GET /data/quality` — dataset quality/coverage checks

The Odds API documents a UK bookmaker region and a historical odds endpoint; historical access and retention depend on the account/subscription. API-Football's odds history is limited, so durable research data should be captured prospectively rather than assumed to be reconstructable later.

## Phase 6 — Research Factory & Execution-Aware Validation

Phase 6 adds a repeatable research factory at `POST /research/factory`.

Controls include:
- chronological walk-forward retraining
- configurable minimum training sample
- minimum bookmaker count
- maximum odds age
- configurable execution slippage in basis points
- one position per canonical event
- model-vs-market Brier/log-loss comparison
- bootstrap confidence intervals
- CLV tracking
- signal buckets by observed model EV
- leakage audit at `GET /research/leakage`
- historical run summaries at `GET /research/factory/runs` and `/research/benchmark`

The factory is deliberately conservative: it does not turn a backtest into a claim of profitability. Real bookmaker limits, voids, stake restrictions, market suspension, price movement, account-level restrictions and incomplete historical coverage still need to be measured.

### Recommended operating order

1. Capture or import historical odds and settled results.
2. Run `/data/quality` and `/research/leakage`.
3. Run `/research/factory` with realistic minimum-books, freshness and slippage assumptions.
4. Repeat across seasons/leagues and compare out-of-sample model metrics against the market benchmark.
5. Require persistent positive CLV across independent time periods before enabling a scanner signal for paper trading.
6. Continue paper/CLV monitoring before considering any real-money use.

Historical odds availability is dependent on the data provider and plan. The Odds API documents historical snapshots at 10-minute intervals from June 2020 and 5-minute intervals from September 2022 for featured markets, with historical access restricted to paid plans. API-Football documents fixture-linked odds but notes only limited historical odds retention, making prospective capture important. 

## Phase 7: multi-season research factory + validated-signal gate

Phase 7 adds the research controls needed before a live signal can be treated as validated:

- Multi-season result ingestion via `POST /data/results-range`.
- Season-by-season walk-forward diagnostics in `/research/factory`.
- Bootstrap ROI confidence intervals.
- Benjamini-Hochberg false-discovery-rate correction across EV signal buckets.
- Persistent research gate configuration and evaluations:
  - `GET /research/gate`
  - `POST /research/gate/config`
  - `POST /research/gate/evaluate`
- Scanner integration with the gate. The gate is **disabled by default**; when enabled, live scanner opportunities are only promoted when the latest research gate is eligible.
- Conservative default gate requirements: at least 100 bets, 500 out-of-sample predictions, 3 seasons, positive lower confidence bounds for ROI and CLV, and model Brier no worse than the market benchmark.

These are research criteria, not a guarantee of profitability. Real eligibility requires actual historical data and repeated out-of-sample evidence.

### Data coverage caveats
The Odds API documents historical snapshots from June 2020, with 10-minute snapshots initially and 5-minute snapshots from September 2022; historical access is plan-dependent. API-Football documents pre-match odds and result/fixture endpoints, but its ordinary odds history is limited, making prospective capture essential for a durable timestamped dataset.

## Phase 8 — research campaign, stability and gate hardening

Phase 8 adds three production-critical layers:

1. **Multi-season campaign orchestration** (`POST /research/campaign`): coordinates result ingestion, optional historical odds import, the walk-forward factory, and stability analysis across one or more competitions/seasons. It does not bypass provider subscription limits.
2. **Stability analysis** (`POST /research/stability`, `GET /research/stability`): evaluates whether the latest out-of-sample signal is persistent across sufficiently sampled seasons and bookmakers. Segments below the minimum sample are excluded rather than treated as failures.
3. **Hardened validated-signal gate**: live promotion can additionally require stable seasons/bookmakers and a fresh research run. The gate remains disabled by default.

New endpoints:
- `POST /research/campaign`
- `GET /research/campaign/runs`
- `POST /research/stability`
- `GET /research/stability`

The dashboard Research tab now exposes the campaign and stability tests. The campaign requires real provider credentials when result ingestion is enabled.
