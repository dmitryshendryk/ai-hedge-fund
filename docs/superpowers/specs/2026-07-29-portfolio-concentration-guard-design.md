# Portfolio Concentration Guard — Design

Status: draft (awaiting approval)
Date: 2026-07-29
Related: `docs/superpowers/specs/2026-07-05-exhaustion-rotation-overlay-design.md`

## 1. Problem

A $20,000 portfolio built from top-scored Discovery ideas lost $2,027.95 realized over
one month. The loss decomposes as:

| Group | Realized |
|---|---|
| MU, ASML, AMD, LRCX (semis / semicap) | **−$2,418** |
| QQQ, VTI, META, NVDA, LLY, GOOGL, KD | **+$390** |

Four positions, correlation ≈ 0.9, accounted for 119% of the net loss. Per-name selection
was not the failure — every one of those names had genuine fundamental support. The failure
was that **four expressions of one factor were held as if they were four independent bets.**

### Why the existing safeguards did not catch it

Two features are frequently mistaken for a defense against this. Neither is one.

**`_compute_concentration()`** (`discovery_service/__init__.py:177`) aggregates idea scores
by sector across the top 200 *Discovery results*. It answers "how much of the idea list is
Technology?" It has no access to the `positions` table and therefore cannot express
"your capital is 80% in semis." It was structurally incapable of firing here.

**The 🐋 Don't Chase toggle** (`discovery-page.tsx:170`) filters on
`distance_from_whale_entry_pct`, which is
`(current_price / best_entry_vwap - 1) * 100` where `best_entry_vwap` is the cheapest
quarter-VWAP at which *any tracked fund ever opened the position*, sourced from 13F history
going back years (`whale_entry_service/__init__.py:352`). "MU +1,537% vs whale" therefore
means *one fund has held MU since ~2016 and is up 16x* — a statement about that fund's
holding period, not about the quality of today's entry.

This distinction is load-bearing. A `> 20%` hard cap on that metric excludes every
long-term compounder (it would have blocked NVDA and META, two of the five winners) while
admitting names trading *below* institutional accumulation — ADBE at −49%, KMX at −30%.
A screen that only permits stocks which have fallen since smart money bought them is a
value-trap generator, not a risk control. The metric is useful as *context* about
institutional cost basis; it is not an entry-quality gate and must not be promoted to one.

The toggle also defaults to `false`, so it cannot be described as a control the user ignored.

## 2. Goal

Give the system a portfolio-level view of risk it currently lacks: measure factor
concentration over **owned capital**, warn before it is exceeded, and surface a read-only
warning in Discovery when a candidate would deepen an already-crowded exposure.

## 3. Non-goals — hard constraints

Carried forward from the standing project constraint:

- **No change to Discovery scoring, sorting, ranking, default filtering, or pagination.**
  Everything this feature adds to the Discovery page is a read-only badge, exactly as the
  Devil's Advocate overlay is.
- **No repurposing of the existing Don't Chase filter.** Its threshold and semantics stay
  as they are. It is only re-labelled so it reads as what it measures.
- No automated trading, no dollar-denominated position sizing, no rebalancing execution.
  The feature warns; the user decides.
- No new external data provider. Sector and industry come from the yfinance `.info` payload
  already fetched by `fundamentals_service`.

## 4. Design

### 4.1 Grouping granularity

`sector` alone is too coarse for this failure. NVDA, AMD, MU, LRCX, and ASML are all
`"Technology"`, yet NVDA finished **+7%** while LRCX finished **−39%**: AI compute held
while the memory / wafer-fab-equipment capex cycle de-rated. A sector-only guard would have
flagged NVDA as part of the problem and would have missed the sub-group that actually was.

The guard therefore measures **both** levels:

- **sector** — the coarse macro bucket (`Technology`, `Healthcare`, …)
- **industry** — the sub-group that fails together (`Semiconductor Equipment & Materials`,
  `Semiconductors`, `Drug Manufacturers — General`, …)

`industry` is not yet on `CompanyMetrics`; it is an additive field (§6, Task 1).

### 4.2 Weighting

Weights are computed over **market value**, falling back to **cost value** per position when
yfinance cannot price it. Unpriced positions must still contribute to the denominator —
silently dropping them would understate concentration, which is the one error mode this
feature cannot afford. The valuation basis is reported in the response so the UI can
disclose when figures are cost-based.

### 4.3 Thresholds

Chosen so the actual losing book trips the critical tier and a reasonably diversified book
trips nothing:

| Grouping | Warn | Critical |
|---|---|---|
| Sector | ≥ 35% | ≥ 50% |
| Industry | ≥ 25% | ≥ 40% |
| Single position | ≥ 20% | ≥ 30% |

Applied to the losing portfolio (semis ≈ four of five names by capital), both the sector and
industry buckets clear their critical tier — the guard fires loudly, before entry.

Constants live at module scope in `position_service`, following the convention of
`_OVERCROWDING_THRESHOLD_PCT` (`= 30.0`) in `discovery_service`. They are module constants,
not user settings, in this iteration.

### 4.4 Backend surface

New schemas in `app/backend/models/position_schemas.py`:

```python
class ConcentrationBucket(BaseModel):
    """One sector, industry, or single-name bucket's share of portfolio value."""
    name: str
    value: float
    weight_pct: float
    tickers: list[str]
    tier: str  # "ok" | "warn" | "critical"


class PortfolioConcentration(BaseModel):
    """Factor concentration over owned capital, at sector and industry level.

    Broad-market ETFs have no sector in the yfinance payload and land in
    "Unclassified" — correct, since an index fund is not a sector bet.
    """
    total_value: float
    valued_on: str            # "market" | "mixed" | "cost"
    sectors: list[ConcentrationBucket]
    industries: list[ConcentrationBucket]
    positions: list[ConcentrationBucket]
    warnings: list[str]       # human-readable, most severe first
    unclassified_pct: float
    sector_warn_pct: float
    sector_critical_pct: float
    industry_warn_pct: float
    industry_critical_pct: float
```

Example bucket, synthetic values:

```json
{"name": "Semiconductors", "value": 8000.0, "weight_pct": 40.0,
 "tickers": ["MU", "LRCX"], "tier": "critical"}
```

`PortfolioConcentration` becomes an optional field on `PositionListResponse`, mirroring how
`DiscoveryResponse` carries `concentration`. Optional so a `fundamentals_service` outage
degrades to a plain position list rather than a 500.

New endpoint for the pre-trade check:

```
GET /positions/concentration/preview?ticker=MU&amount=4000
```

Returns a `PortfolioConcentration` computed as if `amount` dollars of `ticker` were added,
plus the deltas against the current book. This answers "what does buying this do to me?"
without requiring the user to add the position first.

### 4.5 Frontend surface

**Positions page** — concentration HUD above the holdings table, modelled on the existing
Discovery HUD (`discovery-page.tsx:480`): sector bars, industry bars, and a warning banner
listing every bucket at `warn` or above. `critical` buckets render in the destructive palette.

**Discovery page** — a read-only badge on any idea whose sector *or* industry already sits at
`warn` or above in the user's book, e.g. `⚠ you're 62% Semiconductors`. Fetched once per page
load from the positions concentration endpoint and joined client-side on `idea.sector` /
`idea.industry`. Explicitly:

- no effect on `score`, ordering, or which ideas are returned
- no new server-side filter parameter
- badge absent when there are no positions, or when the endpoint fails

**Don't Chase re-label** — the label and tooltip change from an entry-quality framing to what
the filter measures: *"Only ideas trading within 20% of the cheapest price a tracked fund
established its position at (13F cost basis, often years old)."* Behaviour unchanged.

### 4.6 Deliberately deferred

An honest extension-based entry filter (`pct_above_sma` from
`pricing_service.get_technical_snapshot`, already built for the exhaustion overlay) is the
correct replacement for the entry-quality role Don't Chase was wrongly assumed to fill. It is
deferred to its own spec because it changes which ideas Discovery returns, and that deserves
its own approval rather than riding along here. `technical_exhaustion` already surfaces the
same information as a Devil's Advocate badge in the meantime.

## 5. Correctness risks

| Risk | Mitigation |
|---|---|
| Unpriced positions silently understate concentration | Cost-value fallback; `valued_on` disclosed; dedicated test |
| Missing `industry` from yfinance | Bucketed as `"Unclassified"`, reported via `unclassified_pct`, never dropped from the denominator |
| ETF positions (QQQ, VTI) distort sector weights | No sector in yfinance `.info` → `Unclassified`; documented in the schema docstring |
| Discovery page coupling | Badge is additive and failure-tolerant; overlay isolation verified by grep, as the Devil's Advocate isolation was |
| Divide-by-zero on an empty or fully-unpriced book | Return `None` concentration rather than a zero-weight structure |

## 6. Implementation tasks (TDD)

1. **`industry` on `CompanyMetrics`** — additive optional field in
   `fundamentals_service/_metrics.py`, populated from `info.get("industry")`, mirroring the
   existing `sector` line (`_metrics.py:31,100`). Tests: field present when yfinance supplies
   it, `None` when absent.
2. **`compute_concentration()` in `position_service`** — pure function over
   `(positions, metrics_by_ticker)` returning `PortfolioConcentration`. Tests: sector and
   industry tiering at each threshold boundary; cost-value fallback and `valued_on`;
   unclassified handling; empty book; single-position book.
3. **Wire into `GET /positions`** — optional field on `PositionListResponse`, degrading to
   `None` on `fundamentals_service` failure. Tests: present on success, absent on failure,
   list returned either way.
4. **`GET /positions/concentration/preview`** — projected weights and deltas for a
   hypothetical add. Tests: projection arithmetic; unknown ticker; zero or negative amount
   rejected.
5. **Positions page HUD** — sector and industry bars plus warning banner.
6. **Discovery read-only badge + Don't Chase re-label** — includes the overlay-isolation
   check that no Discovery scoring or filtering path changed.

## 7. Acceptance

- Reconstructing the losing book (MU, ASML, AMD, LRCX, SNDK at their entry weights) yields
  `critical` at both sector and industry level, with a warning naming `Semiconductors` and
  `Semiconductor Equipment & Materials`.
- A book of QQQ, VTI, LLY, META, NVDA yields no `critical` bucket.
- `grep -rn "position_service\|concentration" app/backend/services/discovery_service/`
  shows no scoring or filtering dependency.
- Full backend test suite green.
