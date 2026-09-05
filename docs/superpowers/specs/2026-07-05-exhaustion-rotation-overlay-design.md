# Exhaustion & Rotation Overlay — Design Spec

**Date:** 2026-07-05
**Status:** Approved for planning
**Constraint (verbatim, carried from prior sessions):** *"don't break the current logic of the Discovery. I want the Devil Advocate just mark a suggestion in the UI, but not interfere with the current logic."*

## Problem

Discovery finds high-conviction long setups well, but measures only *conviction*, never *exhaustion*. In the semiconductor drawdown (ASML, MU, SNDK, LRCX), the bullish signals were correct on fundamentals while market structure was overheated: prices stretched far above trend, overbought, and a sector-wide rotation out of semis began. The user held into a 5-10% correction with no warning.

The missing information is **not company news** — it was a sector/factor correction. It lives in price/volume structure (per-ticker exhaustion) and sector breadth (market-wide rotation).

## Goal

Add early-warning signals as a **read-only overlay**. Discovery score, sort, rank, and pagination remain untouched. Warnings surface as Devil's Advocate badges (per-ticker) and a Telegram/in-app alert (market-wide).

Realistic expectation: this catches the *transition* (day 1-2 of a rollover), not a multi-day-ahead prediction. Corrections are not reliably predictable in advance; early reaction is.

## Scope

In scope:
- Per-ticker Devil's Advocate detectors: `technical_exhaustion`, `exhausted_analyst`.
- Market-wide alert rule: `sector_rotation_divergence`.
- Supporting data: RSI computation + a combined technical snapshot in `pricing_service`; `target_mean_price` field in `CompanyMetrics`.

Explicitly OUT of scope (deferred, each needs its own spec):
- Put/Call options divergence — yfinance options data is unreliable and has no historical volume; requires a daily-snapshot table built and populated for ~30 days first.
- Analyst-report NLP — report prose is paywalled; the buildable proxy (price vs. mean target) is included instead as `exhausted_analyst`.
- Folding exhaustion into the composite Discovery score — rejected per the standing constraint; overlay only.

## Architecture

Three independent units, each testable in isolation, all reusing existing infrastructure.

### 1. `pricing_service` — technical snapshot (data layer)

Add a single-fetch snapshot so both per-ticker detectors read from one yfinance history pull (yfinance already rate-limits; a second call per ticker would double load).

```
@dataclass
class TechnicalSnapshot:
    ticker: str
    latest_close: float
    sma200: float
    pct_above_sma: float   # (close/sma200 - 1) * 100; negative below trend
    rsi14: float | None    # None when < 15 bars

async def get_technical_snapshot(ticker: str, sma_days: int = 200) -> TechnicalSnapshot | None
```

- Reuses the existing history-fetch pattern from `_compute_sma_sync` (one `yf.Ticker().history()` call).
- RSI(14) via Wilder's smoothing on the same close series; returns None if insufficient bars.
- Shares the module's semaphore + cooldown circuit breaker.
- `get_sma_cross` remains for the existing `technical_breakdown` rule (no change).

### 2. `devils_advocate_service` — two detectors (per-ticker overlay)

Both follow the existing detector contract: `async def detect_*(ticker) -> list[RedFlagFinding]`, failures return `[]`, wired into the `asyncio.gather` fanout in `get_red_flags`. No change to Discovery.

**`_technical_exhaustion.py`** — reads `get_technical_snapshot`:

| Condition | Severity | Score |
|---|---|---|
| ext > 30% AND RSI > 75 | CRITICAL | 60 |
| ext > 30% OR RSI > 75 | WARNING | 40 |
| ext 20-30% (RSI not extreme) | INFO | 20 |
| ext <= 20% and RSI <= 75 | (no finding) | — |

Headline example: `"Technical exhaustion: +32% above 200d trend, RSI 82 - mean-reversion risk"`. `detail` carries pct_above_sma, rsi14, latest_close, sma200.

**`_exhausted_analyst.py`** — reads `CompanyMetrics` (adds `target_mean_price`) + current price from the snapshot:

| Condition | Severity | Score |
|---|---|---|
| price >= 1.15 x target_mean_price | WARNING | 40 |
| target <= price < 1.15 x target | INFO | 20 |
| price < target, or target missing | (no finding) | — |

Headline: `"Above analyst target: trading at $X vs $Y mean target - limited upside / chased momentum"`.

### 3. `alert_service` — sector rotation rule (market-wide)

**`_rules/sector_rotation_rule.py`** — `async def evaluate(thresholds) -> list[AlertCandidate]`.

- Fetch 3-trading-day return for SMH and IWM via `pricing_service.compute_alpha` / `get_period_return`.
- Trigger: `SMH_3d < -2.0%` AND `IWM_3d > +1.0%`.
- **Dedupe:** market-wide rule has no ticker; the alert service dedupes on `(rule_type, ticker, created_at)`. Use a fixed synthetic ticker `"_MARKET_"` plus a cooldown (suppress re-fire while an episode is active) so it alerts once per rotation, not every 4h scan.
- Message names the user's at-risk holdings: query `positions` (or watchlist) for tickers whose sector is Technology / high-beta, list them in the alert body ("EXIT WATCH: ASML, MU, ...").
- Thresholds configurable via settings, matching the existing rule pattern.

## Data flow

```
Discovery page / Ticker page
        |  (existing) GET /devils_advocate/red_flags/{ticker}
        v
get_red_flags() --gather--> detect_ceo_cfo, detect_altman, detect_beneish,
                            detect_technical_exhaustion, detect_exhausted_analyst
                                    |                        |
                                    v                        v
                        pricing_service.get_technical_snapshot   fundamentals get_company_metrics (+target_mean_price)

AlertScheduler (4h) --> sector_rotation_rule.evaluate()
                            |  SMH/IWM 3d returns via pricing_service
                            v
                        AlertCandidate("_MARKET_", cooldown-gated) --> Telegram + in-app feed
```

## Error handling

- Any detector failure -> `[]` (existing gather already absorbs exceptions; report degrades per-finding, never fails).
- yfinance unavailable / cooldown -> snapshot returns None -> detectors emit nothing (no false "safe" claim, just absence).
- Sector rule: if either ETF fails to price, skip the scan silently (log at debug), retry next cycle.
- Missing `target_mean_price` -> `exhausted_analyst` emits nothing.

## Testing

- `pricing_service`: RSI math against a known series; snapshot returns None on short history.
- Detectors: mock-inject snapshots — CRITICAL fires at ext>30%+RSI>75; WARNING at either; INFO band; nothing when calm. `exhausted_analyst` tiers by price/target ratio. Technicals apply to all sectors (no financial-sector exclusion needed).
- Sector rule: mock SMH/IWM returns — fires only when both legs cross; cooldown suppresses second fire in same episode.

## Files touched

| File | Change |
|---|---|
| `pricing_service/__init__.py` | add `TechnicalSnapshot`, `get_technical_snapshot`, RSI helper |
| `fundamentals_service/_metrics.py` | add `target_mean_price` (from `info["targetMeanPrice"]`) |
| `devils_advocate_service/_technical_exhaustion.py` | new detector |
| `devils_advocate_service/_exhausted_analyst.py` | new detector |
| `devils_advocate_service/__init__.py` | wire both into `get_red_flags` gather |
| `alert_service/_rules/sector_rotation_rule.py` | new market-wide rule |
| `alert_service/_rules/__init__.py` | register rule + thresholds extractor |

No migration (no new tables). No frontend change required — the DA badge column already renders new findings; the alert already surfaces in the notification feed.

## How it solves the semiconductor problem

Had this been live, the Discovery page overlay would have shown ASML/MU flagged CRITICAL technical exhaustion (price far above trend, overbought) beside their high bullish scores — the bull thesis intact, the entry timing flagged as a trap — while a clean name showed no red flag. Concurrently, once SMH rolled over versus IWM, a single sector-rotation alert would have named the at-risk holdings and enabled exit watch. Not a prediction days ahead; a same-day structural warning at the start of the move.
