# Portfolio Concentration Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-29-portfolio-concentration-guard-design.md`

**Goal:** Measure factor concentration over owned capital at sector *and* industry granularity, warn before a threshold is exceeded, and answer "what does buying this do to me?" before the trade — without altering Discovery scoring, sorting, or filtering.

**Architecture:** One pure function (`compute_concentration`) over already-enriched `PositionResponse` rows plus a `CompanyMetrics` lookup, exposed as an optional field on the existing `GET /positions` response and via one new preview endpoint. Frontend adds a HUD to the Positions page and a read-only badge to Discovery. No new tables, no new migration, no new external data source.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, Pydantic v2, pytest, React + Vite + TypeScript, Tailwind, shadcn/ui.

## Global Constraints

- Discovery score, sort, rank, default filtering, and pagination MUST remain untouched. Everything added to the Discovery page is a read-only badge. (Verbatim: *"don't break the current logic of the Discovery. I want the Devil Advocate just mark a suggestion in the UI, but not interfere with the current logic."*)
- The existing 🐋 Don't Chase filter keeps its threshold and behaviour. Task 6 changes only its label and tooltip text.
- Unpriced positions MUST remain in the concentration denominator via cost-value fallback. Excluding them understates concentration — the one error this feature cannot make. This deliberately differs from `list_positions_enriched`, which *does* exclude unpriced names from market-value totals; that is right for P&L and wrong for risk.
- Run tests with `poetry run pytest` (NOT `uv` — this project uses Poetry).
- Black line length 420; type hints use `X | None`, not `Optional[X]`.
- No new yfinance calls: `compute_concentration` is pure and receives its metrics from the caller, which already batches them.
- Every backend task ends with the full backend suite green, not just its own file.

---

### Task 1: `industry` field on `CompanyMetrics`

**Files:**
- Modify: `app/backend/services/fundamentals_service/_metrics.py`
- Test: `tests/backend/services/test_fundamentals_industry.py`

**Interfaces:**
- Consumes: the existing yfinance `.info` dict already passed to `_build_metrics_from_info`.
- Produces: `CompanyMetrics.industry: str | None = None`, populated from `info.get("industry") or None`.

Mirror the existing `sector` field exactly — declaration alongside `sector` (`_metrics.py:31`) and population alongside `sector=(info.get("sector") or None)` (`_metrics.py:100`). Additive only; no existing consumer changes.

- [ ] **Step 1: Write the failing test**

```python
# tests/backend/services/test_fundamentals_industry.py
from app.backend.services.fundamentals_service._metrics import _build_metrics_from_info


def test_industry_populated_when_present():
    m = _build_metrics_from_info("LRCX", {"industry": "Semiconductor Equipment & Materials"})
    assert m.industry == "Semiconductor Equipment & Materials"


def test_industry_none_when_absent():
    assert _build_metrics_from_info("XYZ", {}).industry is None


def test_industry_none_when_empty_string():
    # yfinance sometimes returns "" rather than omitting the key.
    assert _build_metrics_from_info("XYZ", {"industry": ""}).industry is None
```

- [ ] **Step 2: Run the test, confirm it fails on the missing attribute**
- [ ] **Step 3: Add the field and its population line**
- [ ] **Step 4: `poetry run pytest tests/backend/services/test_fundamentals_industry.py -v` → 3 passed**
- [ ] **Step 5: Full backend suite green**
- [ ] **Step 6: Commit** — `feat(fundamentals): add industry field to CompanyMetrics`

---

### Task 2: `compute_concentration()` — the pure core

**Files:**
- Modify: `app/backend/models/position_schemas.py`
- Modify: `app/backend/services/position_service/__init__.py`
- Test: `tests/backend/services/test_position_concentration.py`

**Interfaces:**
- Consumes: `list[PositionResponse]` (already carries `market_value` — `None` when unpriced — and `cost_value`), plus `dict[str, CompanyMetrics | None]` keyed by upper-case ticker.
- Produces:
  - `ConcentrationBucket` and `PortfolioConcentration` schemas exactly as specified in design §4.4.
  - `def compute_concentration(items: list[PositionResponse], metrics_by_ticker: dict) -> PortfolioConcentration | None`

**Module constants** (naming follows `_OVERCROWDING_THRESHOLD_PCT` in `discovery_service`):

```python
_SECTOR_WARN_PCT: float = 35.0
_SECTOR_CRITICAL_PCT: float = 50.0
_INDUSTRY_WARN_PCT: float = 25.0
_INDUSTRY_CRITICAL_PCT: float = 40.0
_POSITION_WARN_PCT: float = 20.0
_POSITION_CRITICAL_PCT: float = 30.0
_UNCLASSIFIED = "Unclassified"
```

**Semantics:**
- Per-position value = `market_value` when not `None`, else `cost_value`.
- `valued_on` = `"market"` when every position is priced, `"cost"` when none is, `"mixed"` otherwise.
- `total_value` = sum of per-position values. Return `None` when there are no positions or `total_value <= 0`.
- Buckets sorted by `weight_pct` descending. `tier` from the matching threshold pair, using `>=`.
- `warnings` ordered critical-first then warn, each naming the bucket and its weight, e.g. `"Semiconductors is 40% of your book (critical)"`.
- `unclassified_pct` = share of `total_value` whose `sector` is missing. Unclassified value stays in `total_value` and appears as an `Unclassified` bucket, but is never tiered (always `"ok"`) — an index-fund sleeve is not a concentrated bet.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backend/services/test_position_concentration.py
from app.backend.models.position_schemas import PositionResponse
from app.backend.services.position_service import compute_concentration


class _M:
    def __init__(self, sector=None, industry=None):
        self.sector = sector
        self.industry = industry


def _pos(ticker, market_value, cost_value=None):
    cv = cost_value if cost_value is not None else market_value
    return PositionResponse(
        id=1, ticker=ticker, shares=1.0, cost_basis=cv, entry_date="2026-06-29",
        cost_value=cv, market_value=market_value,
    )


def _bucket(buckets, name):
    return next(b for b in buckets if b.name == name)


def test_returns_none_for_empty_book():
    assert compute_concentration([], {}) is None


def test_the_losing_book_trips_critical_at_both_levels():
    # Reconstruction of the actual losing portfolio: four semis, one non-semi.
    items = [
        _pos("MU", 4000.0), _pos("ASML", 4000.0),
        _pos("AMD", 4000.0), _pos("LRCX", 4000.0),
        _pos("LLY", 4000.0),
    ]
    metrics = {
        "MU": _M("Technology", "Semiconductors"),
        "AMD": _M("Technology", "Semiconductors"),
        "ASML": _M("Technology", "Semiconductor Equipment & Materials"),
        "LRCX": _M("Technology", "Semiconductor Equipment & Materials"),
        "LLY": _M("Healthcare", "Drug Manufacturers - General"),
    }
    c = compute_concentration(items, metrics)
    tech = _bucket(c.sectors, "Technology")
    assert tech.weight_pct == 80.0
    assert tech.tier == "critical"
    semis = _bucket(c.industries, "Semiconductors")
    assert semis.weight_pct == 40.0
    assert semis.tier == "critical"
    assert any("Technology" in w for w in c.warnings)
    assert c.valued_on == "market"


def test_diversified_book_trips_nothing():
    items = [_pos(t, 4000.0) for t in ("QQQ", "VTI", "LLY", "META", "NVDA")]
    metrics = {
        "QQQ": _M(None, None), "VTI": _M(None, None),
        "LLY": _M("Healthcare", "Drug Manufacturers - General"),
        "META": _M("Communication Services", "Internet Content & Information"),
        "NVDA": _M("Technology", "Semiconductors"),
    }
    c = compute_concentration(items, metrics)
    assert all(b.tier != "critical" for b in c.sectors + c.industries)
    assert c.unclassified_pct == 40.0


def test_unpriced_position_falls_back_to_cost_and_stays_in_denominator():
    items = [_pos("MU", None, cost_value=6000.0), _pos("LLY", 4000.0)]
    metrics = {"MU": _M("Technology", "Semiconductors"), "LLY": _M("Healthcare", "Drug")}
    c = compute_concentration(items, metrics)
    assert c.total_value == 10000.0
    assert c.valued_on == "mixed"
    assert _bucket(c.sectors, "Technology").weight_pct == 60.0


def test_all_unpriced_reports_cost_basis():
    items = [_pos("MU", None, cost_value=5000.0)]
    c = compute_concentration(items, {"MU": _M("Technology", "Semiconductors")})
    assert c.valued_on == "cost"


def test_single_position_book_flags_position_concentration():
    c = compute_concentration([_pos("MU", 5000.0)], {"MU": _M("Technology", "Semiconductors")})
    assert _bucket(c.positions, "MU").tier == "critical"


def test_threshold_boundaries_are_inclusive():
    # Exactly 35% sector -> warn, not ok.
    items = [_pos("A", 3500.0), _pos("B", 6500.0)]
    metrics = {"A": _M("Technology", "Semiconductors"), "B": _M("Healthcare", "Drug")}
    c = compute_concentration(items, metrics)
    assert _bucket(c.sectors, "Technology").tier == "warn"


def test_missing_metrics_entry_is_unclassified_not_a_crash():
    c = compute_concentration([_pos("ZZZZ", 1000.0)], {})
    assert c.unclassified_pct == 100.0
```

- [ ] **Step 2: Run tests, confirm they fail on the missing import**
- [ ] **Step 3: Add the two schemas to `position_schemas.py`** per design §4.4, with docstrings covering the ETF/`Unclassified` behaviour and the cost-fallback rule
- [ ] **Step 4: Implement `compute_concentration`** — pure, no DB, no network, not `async`
- [ ] **Step 5: `poetry run pytest tests/backend/services/test_position_concentration.py -v` → all green**
- [ ] **Step 6: Full backend suite green**
- [ ] **Step 7: Commit** — `feat(positions): add portfolio concentration computation`

---

### Task 3: Wire concentration into `GET /positions`

**Files:**
- Modify: `app/backend/models/position_schemas.py` (add the optional field)
- Modify: `app/backend/services/position_service/__init__.py` (`list_positions_enriched`)
- Test: `tests/backend/services/test_position_concentration_wiring.py`

**Interfaces:**
- Produces: `PositionListResponse.concentration: PortfolioConcentration | None = None`
- `list_positions_enriched` gains one `get_company_metrics_batch(tickers)` call, wrapped so any failure logs at debug and leaves `concentration=None`.

The metrics fetch must not be able to break the position list — that list is the page's primary content and already degrades gracefully when pricing fails.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backend/services/test_position_concentration_wiring.py
# Patch pricing_service.compute_alpha_batch and
# fundamentals_service.get_company_metrics_batch with patch.object on the
# modules the service imports from, following the existing test convention.
#
# Cases:
#   - metrics available          -> response.concentration is not None
#   - metrics raise              -> concentration is None, items still returned
#   - metrics return empty dict  -> concentration present, unclassified_pct == 100
#   - empty positions table      -> concentration is None, no metrics call made
```

- [ ] **Step 2: Run, confirm failure**
- [ ] **Step 3: Add the field, then the guarded call in `list_positions_enriched`**
- [ ] **Step 4: Tests green**
- [ ] **Step 5: Full backend suite green**
- [ ] **Step 6: Commit** — `feat(positions): expose concentration on the positions list`

---

### Task 4: `GET /positions/concentration/preview`

**Files:**
- Modify: `app/backend/models/position_schemas.py`
- Modify: `app/backend/services/position_service/__init__.py`
- Modify: `app/backend/routes/positions.py`
- Test: `tests/backend/routes/test_positions_preview.py`

**Interfaces:**
- Produces schema:

```python
class ConcentrationPreviewResponse(BaseModel):
    """Projected concentration if `amount` dollars of `ticker` were added.

    Weights are before/after for the buckets the candidate lands in, so the
    caller can state the marginal effect of the trade directly.
    """
    ticker: str
    amount: float
    sector: str | None
    industry: str | None
    sector_weight_before_pct: float
    sector_weight_after_pct: float
    industry_weight_before_pct: float
    industry_weight_after_pct: float
    resulting_tier: str            # worst tier the candidate's buckets reach after the add
    projected: PortfolioConcentration
```

- Produces service fn: `async def preview_concentration(db, ticker: str, amount: float) -> ConcentrationPreviewResponse`
- Produces route: `GET /positions/concentration/preview?ticker=…&amount=…`

Implementation: build the current enriched list, append a synthetic `PositionResponse` for the candidate (`market_value = cost_value = amount`), fetch metrics for held tickers *plus* the candidate in one batch, then run `compute_concentration` twice — once without and once with the synthetic row.

Route declaration order matters: register this static path **above** the `/{ticker}` routes so `"concentration"` cannot be captured as a ticker. The file currently has `PUT`/`DELETE` on `/{ticker}` but no `GET /{ticker}`, so there is no live conflict — order it correctly anyway.

- [ ] **Step 1: Write the failing tests** — projection arithmetic (adding $4k of MU to a $16k book holding $8k of semis moves the industry bucket from 50% to 60%); unknown ticker with no metrics → `sector: None`, `Unclassified` bucket, still HTTP 200; `amount <= 0` → HTTP 400; empty ticker → HTTP 400; empty book → before weights 0, after 100
- [ ] **Step 2: Run, confirm failure**
- [ ] **Step 3: Implement schema, service fn, route**
- [ ] **Step 4: Tests green**
- [ ] **Step 5: Full backend suite green**
- [ ] **Step 6: Commit** — `feat(positions): add concentration preview endpoint`

---

### Task 5: Positions page concentration HUD

**Files:**
- Modify: `app/frontend/src/services/position-api.ts`
- Modify: `app/frontend/src/pages/positions-page.tsx`

**Interfaces:**
- Consumes: `concentration` on the positions list response.
- Produces: TS mirrors of `ConcentrationBucket` / `PortfolioConcentration`; a HUD block above the holdings table.

Model the HUD on the existing Discovery concentration HUD (`discovery-page.tsx:480-525`) so the two read consistently: a labelled bar per bucket, `critical` in the destructive palette, `warn` in the muted-warning palette. Sector bars and industry bars as separate rows. Above them, a banner listing `warnings` when non-empty. When `valued_on !== "market"`, add a one-line disclosure that some positions are valued at cost. Suppress the whole block when `concentration` is `null`.

- [ ] **Step 1: Add the TS interfaces to `position-api.ts`**
- [ ] **Step 2: Render the HUD**
- [ ] **Step 3: Verify build** — `npx --prefix app/frontend tsc --noEmit`, then `npx --prefix app/frontend vite build`
- [ ] **Step 4: Commit** — `feat(positions): add concentration HUD to positions page`

---

### Task 6: Discovery read-only badge + Don't Chase re-label

**Files:**
- Modify: `app/backend/models/discovery_schemas.py` (add `industry` to `DiscoveryIdea`)
- Modify: `app/backend/services/discovery_service/__init__.py` (populate `idea.industry` in the existing alpha enricher, beside `idea.sector = cm.sector` at lines 169-170)
- Modify: `app/frontend/src/services/discovery-api.ts`
- Modify: `app/frontend/src/pages/discovery-page.tsx`

**Interfaces:**
- Consumes: the positions concentration endpoint, fetched once per Discovery page load.
- Produces: a read-only badge on ideas whose `sector` or `industry` sits at `warn` or above in the user's book.

**This is the task where the global constraint is most at risk.** Permitted: adding an optional `industry` field, populating it in the enricher that already populates `sector`, and rendering a badge. Forbidden: touching `score`, the sort comparator, `kill_filter`, pagination, `_compute_concentration`, or adding any server-side filter parameter.

Badge behaviour: absent when the user holds nothing, when the endpoint fails, or when the idea's buckets are all `ok`. Failure is silent — a concentration outage must not degrade Discovery.

Don't Chase re-label — label becomes `🐋 Within 20% of whale cost basis`, tooltip becomes:

> Only ideas trading within 20% of the cheapest price a tracked fund established its position at. This is 13F cost basis, often years old — a high number means a fund has held the name a long time, not that today's entry is poor.

Threshold and filter behaviour unchanged.

- [ ] **Step 1: Add `industry` to `DiscoveryIdea` + enricher + TS interface**
- [ ] **Step 2: Fetch concentration on Discovery load, guarded so failure is a no-op**
- [ ] **Step 3: Render the badge**
- [ ] **Step 4: Re-label the toggle**
- [ ] **Step 5: Overlay isolation check** — `grep -rn "position_service\|concentration/preview" app/backend/services/discovery_service/` returns nothing; `git diff` on `_engine.py` and `_sources/` is empty
- [ ] **Step 6: Frontend build clean + full backend suite green**
- [ ] **Step 7: Commit** — `feat(discovery): add read-only concentration badge, clarify whale filter label`

---

## Final Verification

- [ ] Acceptance criteria from design §7 all demonstrated by passing tests
- [ ] `git diff <task-1-base>..HEAD -- app/backend/services/discovery_service/` shows only the additive `industry` population
- [ ] Full backend suite green; frontend `tsc --noEmit` and `vite build` clean
- [ ] `.superpowers/sdd/progress.md` updated with per-task commits and any findings
