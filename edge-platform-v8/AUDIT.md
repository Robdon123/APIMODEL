# Audit of the current system (Phase 1 of the master prompt)
Scope: `edge-finder.html` (static app) and `edge-platform/app.py` + `dashboard.html`. Nothing here has been run against live provider data.

## Verified by tests (tests/test_math.py, 18 tests)
De-margining sums to 1 and keeps ordering; Kelly formula, fractions and cap; Dixon-Coles-style matrix sums to 1 and totals are monotone;
UFC method/round probabilities partition to 1; consensus sums to 1 and excludes sharp books from best price.

## Bugs found and fixed
1. Static app: a variable shadowed the event object, so fixture names would print "undefined v undefined". Fixed earlier.
2. Backend: one-outcome markets got fair probability 1.0 (huge fake EV). Found by test, fixed (`consensus` skips them).

## Phase 2 fixes applied
1. CLV close now rejects snapshots after kick-off.
2. Paper-bet lifecycle events are append-only in `immutable_events`; prediction rows are not updated for close/result state.
3. Added automated scanner persistence, scheduler controls, scan history and alert history.
4. Added CORS support for a separate browser dashboard and scanner configuration endpoints.

## Remaining research limitations
- No live provider response has been verified in this environment.
- UK bookmaker coverage is limited to whatever legitimate feeds return; the system must not claim universal bookmaker coverage unless the connected feed actually supplies it.
- The scanner automates the existing market-consensus engine; it does not create a proven predictive edge.
- Independent fitted football/UFC models, historical walk-forward testing, calibration, uncertainty intervals and learned bookmaker weights remain research phases requiring data.

## Methodological limits (not bugs, but they cap what can be claimed)
- The football "Dixon-Coles" is NOT fitted: it converts user-typed xG into Poisson rates with a fixed rho (-0.08) and home advantage. No time decay, no estimation, no league effects. It should be renamed "xG-Poisson" until a fitted model exists.
- The UFC model takes hand-entered win probability and method splits; finish-round weights are generic placeholders, not fitted.
- "Fair" probability is market consensus only. There is no independent model, so "edge" = disagreement between the best soft price and consensus, not a validated forecast advantage.
- Confidence = f(number of books, dispersion) with hand-picked constants (8 books, 0.04 sd). It is a heuristic, not a calibrated confidence.
- EV has no uncertainty adjustment, exchange commission, slippage, stale-odds check or correlation handling.
- Only the power de-margin method; no Shin/proportional comparison; no learned source weights (sharp = 3x is arbitrary).
- No team/fighter name normalisation across providers; backend `latest_rows` uses the newest batch per sport only, so providers cannot be cross-checked.

## Provider integrations (unverified)
API-Football bet names/paths and API-Sports MMA odds path (`/odds?fight=`) and response shape were written from documentation snippets and memory. Treat as NOT TESTED.

## Security
- Static app: API key is typed into the browser and stored in localStorage; it is visible to any script on the page and in network calls. Acceptable only for personal use; never hard-code a key.
- Backend: no authentication or rate limiting on `/ingest` (anyone with the URL can burn provider quota), no CORS policy, no key redaction in logs.
- No PostgreSQL, migrations, structured logging or health endpoint.

## Duplication
Consensus/EV/Kelly exist in both JS and Python and can drift; the backend should be the single source of truth.

## Proposed order of work
2 fix the two known defects + add schema/immutability -> 3 refactor into modules (providers, markets, forecasting, portfolio, database) ->
4 canonical event/odds schema with timestamps -> 5 baselines and a genuinely fitted Dixon-Coles/Elo -> 6-8 calibration, vig-method comparison, walk-forward backtest (needs historical odds data) -> 9+ portfolio, UI, monitoring, deployment.

## Phase 6 additions

- Added research factory with strict chronological walk-forward retraining.
- Added execution-aware slippage assumption.
- Added minimum bookmaker and maximum odds-age controls.
- Enforced one candidate position per canonical event in factory backtests.
- Added model-vs-market probability benchmarks.
- Added signal-bucket diagnostics.
- Added leakage audit endpoint checking future odds, future outcomes and duplicate canonical outcomes.
- Added research factory run history and benchmark endpoints.
- Added 4 Phase 6 tests; full pytest suite is 30 passing tests.

Known limitation: synthetic validation confirms implementation, not betting profitability. Real historical bookmaker coverage, account limits, void rules, stake restrictions and execution latency remain external variables.

## Phase 7 additions

- Added multi-season result ingestion endpoint.
- Added season-level research factory diagnostics.
- Added bootstrap confidence intervals for ROI.
- Added Benjamini-Hochberg correction across tested EV buckets to reduce false-discovery risk.
- Added persistent validated-signal research gate with configurable thresholds.
- Gate defaults to disabled and is never treated as proof of future profitability.
- Live scanner can be configured to require an eligible gate before emitting opportunities.
- Added Phase 7 tests; full suite passes.

## Phase 8 additions

- Added reproducible multi-season campaign orchestration.
- Added season-level and bookmaker-level stability analysis with minimum-sample exclusions.
- Hardened the validated-signal gate with stability requirements and research-run freshness.
- Added backward-compatible gate-schema migration for existing databases.
- Added Phase 8 automated tests covering campaign input validation, stability analysis, and gate enforcement.

Important: campaign and stability outputs remain research diagnostics. They do not establish future profitability and do not guarantee that a displayed bookmaker price remains obtainable.
