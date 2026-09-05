# Exhaustion & Rotation Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only early-warning signals (per-ticker technical exhaustion + market-wide sector rotation) that surface as Devil's Advocate badges and a Telegram/in-app alert, without touching Discovery scoring.

**Architecture:** Three independent units reusing existing infrastructure — a technical snapshot in `pricing_service` (RSI + SMA extension from one yfinance fetch), two new Devil's Advocate detectors wired into the existing `get_red_flags` gather, and one new market-wide alert rule registered in the alert service. No new tables, no frontend changes.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, yfinance, pandas, pytest, Pydantic v2.

## Global Constraints

- Discovery score, sort, rank, and pagination MUST remain untouched. This is overlay-only. (Verbatim: *"don't break the current logic of the Discovery. I want the Devil Advocate just mark a suggestion in the UI, but not interfere with the current logic."*)
- Black line length 420; type hints use `X | None`, not `Optional[X]`.
- Detectors follow the existing contract: `async def detect_*(ticker: str) -> list[RedFlagFinding]`, failures return `[]`, never raise into the gather.
- Run tests with `poetry run pytest` (NOT `uv` — this project uses Poetry).
- Reuse a single yfinance history fetch per ticker; do not add a second network call per detector.
- Severity tiers (existing `Severity` enum): CRITICAL score 60, WARNING 40, INFO 20.

---

### Task 1: RSI helper + technical snapshot in pricing_service

**Files:**
- Modify: `app/backend/services/pricing_service/__init__.py`
- Test: `tests/backend/services/test_pricing_technical_snapshot.py`

**Interfaces:**
- Consumes: existing `yf.Ticker().history()` fetch pattern from `_compute_sma_sync`, module `_yfinance_semaphore`, `_is_in_cooldown`, `_trigger_cooldown`, `_RateLimited`.
- Produces:
  - `_compute_rsi(closes: "pd.Series", period: int = 14) -> float | None` — Wilder's RSI on a close series; None if fewer than `period + 1` finite closes.
  - `@dataclass TechnicalSnapshot(ticker: str, latest_close: float, sma200: float, pct_above_sma: float, rsi14: float | None)`
  - `async def get_technical_snapshot(ticker: str, sma_days: int = 200) -> TechnicalSnapshot | None`

- [ ] **Step 1: Write the failing test for RSI math**

```python
# tests/backend/services/test_pricing_technical_snapshot.py
import pandas as pd
import pytest

from app.backend.services.pricing_service import _compute_rsi


def test_rsi_all_gains_approaches_100():
    # Monotonically rising series -> RSI near 100 (no losses).
    closes = pd.Series([float(x) for x in range(1, 30)])
    rsi = _compute_rsi(closes, period=14)
    assert rsi is not None
    assert rsi > 99.0


def test_rsi_insufficient_data_returns_none():
    closes = pd.Series([1.0, 2.0, 3.0])
    assert _compute_rsi(closes, period=14) is None


def test_rsi_known_series_midrange():
    # Alternating up/down of equal size -> RSI hovers near 50.
    vals = []
    price = 100.0
    for i in range(40):
        price += 1.0 if i % 2 == 0 else -1.0
        vals.append(price)
    rsi = _compute_rsi(pd.Series(vals), period=14)
    assert rsi is not None
    assert 30.0 < rsi < 70.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/backend/services/test_pricing_technical_snapshot.py -v`
Expected: FAIL with `ImportError: cannot import name '_compute_rsi'`

- [ ] **Step 3: Implement `_compute_rsi`**

Add near the other sync helpers in `pricing_service/__init__.py`:

```python
def _compute_rsi(closes: "pd.Series", period: int = 14) -> float | None:
    """Wilder's RSI on a close-price series. None when < period+1 finite closes.

    Uses exponential (Wilder) smoothing rather than a simple rolling mean so
    the value matches what charting tools report.
    """
    series = closes.dropna()
    if len(series) < period + 1:
        return None
    delta = series.diff().dropna()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    avg_gain = gains.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    avg_loss = losses.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    if not math.isfinite(rsi):
        return None
    return round(float(rsi), 2)
```

`math` is already imported in this module. Ensure `import pandas as pd` is present at the top (add it if not).

- [ ] **Step 4: Run RSI tests to verify they pass**

Run: `poetry run pytest tests/backend/services/test_pricing_technical_snapshot.py -v`
Expected: 3 PASS

- [ ] **Step 5: Write the failing test for the snapshot**

Append to the same test file. Patch the module-level `yf` so no network is hit:

```python
from unittest.mock import patch

import app.backend.services.pricing_service as ps


class _FakeTicker:
    def __init__(self, frame):
        self._frame = frame

    def history(self, start=None, end=None, auto_adjust=True):
        return self._frame


def _rising_frame(n=260):
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    closes = [100.0 + i for i in range(n)]  # steady uptrend
    return pd.DataFrame({"Close": closes}, index=idx)


@pytest.mark.asyncio
async def test_technical_snapshot_uptrend():
    frame = _rising_frame()
    with patch.object(ps.yf, "Ticker", return_value=_FakeTicker(frame)):
        snap = await ps.get_technical_snapshot("TEST", sma_days=200)
    assert snap is not None
    assert snap.ticker == "TEST"
    assert snap.latest_close > snap.sma200          # price above its own SMA
    assert snap.pct_above_sma > 0
    assert snap.rsi14 is not None and snap.rsi14 > 90.0  # relentless uptrend


@pytest.mark.asyncio
async def test_technical_snapshot_short_history_returns_none():
    short = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0]},
        index=pd.date_range("2025-01-01", periods=3, freq="D"),
    )
    with patch.object(ps.yf, "Ticker", return_value=_FakeTicker(short)):
        snap = await ps.get_technical_snapshot("TEST", sma_days=200)
    assert snap is None
```

- [ ] **Step 6: Run to verify it fails**

Run: `poetry run pytest tests/backend/services/test_pricing_technical_snapshot.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'get_technical_snapshot'`

- [ ] **Step 7: Implement `TechnicalSnapshot` + `get_technical_snapshot`**

Add the dataclass near `SmaCross`:

```python
@dataclass
class TechnicalSnapshot:
    """Single-fetch technical read for exhaustion detectors."""
    ticker: str
    latest_close: float
    sma200: float
    pct_above_sma: float  # (close/sma - 1) * 100; negative below trend
    rsi14: float | None
```

Add the sync worker + async wrapper (mirror `_compute_sma_sync` / `get_sma_cross`):

```python
def _compute_technical_snapshot_sync(ticker: str, days: int) -> TechnicalSnapshot | None:
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=int(days * 1.6) + 30)
    try:
        history = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
    except Exception as exc:
        msg = str(exc)
        if _is_rate_limit_message(msg):
            raise _RateLimited(msg) from exc
        logger.warning("pricing_service technical snapshot failed for %s: %s", ticker, exc)
        return None
    if history is None or history.empty:
        return None
    try:
        closed = history.dropna(subset=["Close"])
    except KeyError:
        return None
    if len(closed) < days:
        return None

    closes = closed["Close"]
    window = closes.tail(days)
    try:
        sma = float(window.mean())
        latest = float(closes.iloc[-1])
    except (ValueError, TypeError):
        return None
    if not (math.isfinite(sma) and math.isfinite(latest)) or sma <= 0:
        return None
    return TechnicalSnapshot(
        ticker=ticker.upper(),
        latest_close=latest,
        sma200=sma,
        pct_above_sma=(latest / sma - 1.0) * 100.0,
        rsi14=_compute_rsi(closes),
    )


async def get_technical_snapshot(ticker: str, sma_days: int = 200) -> TechnicalSnapshot | None:
    """Single-fetch snapshot: latest close, N-day SMA + extension, and RSI(14).
    Used by the technical_exhaustion Devil's Advocate detector. Shares the
    global yfinance cooldown / semaphore guard.
    """
    if _is_in_cooldown():
        return None
    async with _yfinance_semaphore:
        try:
            return await asyncio.to_thread(_compute_technical_snapshot_sync, ticker.upper(), sma_days)
        except _RateLimited:
            _trigger_cooldown()
            return None
```

- [ ] **Step 8: Run to verify snapshot tests pass**

Run: `poetry run pytest tests/backend/services/test_pricing_technical_snapshot.py -v`
Expected: all PASS

- [ ] **Step 9: Commit**

```bash
git add app/backend/services/pricing_service/__init__.py tests/backend/services/test_pricing_technical_snapshot.py
git commit -m "feat(pricing): add RSI + technical snapshot for exhaustion detectors"
```

---

### Task 2: Add target_mean_price to CompanyMetrics

**Files:**
- Modify: `app/backend/services/fundamentals_service/_metrics.py`
- Test: `tests/backend/services/test_metrics_target_price.py`

**Interfaces:**
- Consumes: existing `CompanyMetrics` dataclass, the `info` dict from yfinance, and `safe_float`.
- Produces: `CompanyMetrics.target_mean_price: float | None` populated from `info["targetMeanPrice"]`.

**PRE-STEP (read first):** Open `app/backend/services/fundamentals_service/_metrics.py` around lines 25-110. Identify (a) the `CompanyMetrics` dataclass definition and (b) the function that builds it from an `info` dict (it sets `market_cap = safe_float(info.get("marketCap"))` and returns `CompanyMetrics(...)`). Note the exact builder name and signature — the test in Step 1 must call the real builder. If it differs from `_build_metrics_from_info(ticker, info)`, update the two test calls accordingly while keeping the assertions identical.

- [ ] **Step 1: Write the failing test**

```python
# tests/backend/services/test_metrics_target_price.py
# NOTE: replace `_build_metrics_from_info` / call shape with the real builder
# identified in the PRE-STEP if it differs.
from app.backend.services.fundamentals_service._metrics import _build_metrics_from_info


def test_target_mean_price_parsed():
    info = {"longName": "Test Co", "marketCap": 1_000_000_000, "targetMeanPrice": 250.5}
    m = _build_metrics_from_info("TEST", info)
    assert m.target_mean_price == 250.5


def test_target_mean_price_missing_is_none():
    info = {"longName": "Test Co", "marketCap": 1_000_000_000}
    m = _build_metrics_from_info("TEST", info)
    assert m.target_mean_price is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/backend/services/test_metrics_target_price.py -v`
Expected: FAIL (`AttributeError: 'CompanyMetrics' object has no attribute 'target_mean_price'`)

- [ ] **Step 3: Add the field + parse**

In `_metrics.py`, add to the `CompanyMetrics` dataclass near `market_cap`:

```python
    target_mean_price: float | None = None
```

In the builder where `market_cap = safe_float(info.get("marketCap"))` is set, add:

```python
    target_mean_price = safe_float(info.get("targetMeanPrice"))
```

and pass `target_mean_price=target_mean_price` into the `CompanyMetrics(...)` constructor.

- [ ] **Step 4: Run to verify it passes**

Run: `poetry run pytest tests/backend/services/test_metrics_target_price.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/backend/services/fundamentals_service/_metrics.py tests/backend/services/test_metrics_target_price.py
git commit -m "feat(fundamentals): expose targetMeanPrice on CompanyMetrics"
```

---

### Task 3: technical_exhaustion detector

**Files:**
- Create: `app/backend/services/devils_advocate_service/_technical_exhaustion.py`
- Test: `tests/backend/services/devils_advocate/test_technical_exhaustion.py`

**Interfaces:**
- Consumes: `pricing_service.get_technical_snapshot` (Task 1), `RedFlagFinding`, `Severity`.
- Produces: `async def detect_technical_exhaustion(ticker: str) -> list[RedFlagFinding]`.

**Thresholds (from spec):** ext = `pct_above_sma`. CRITICAL(60) when ext>30 AND rsi>75; WARNING(40) when ext>30 OR rsi>75; INFO(20) when 20<ext<=30 and not already warned; else no finding. RSI None is treated as "not overbought".

- [ ] **Step 1: Write the failing tests**

```python
# tests/backend/services/devils_advocate/test_technical_exhaustion.py
from unittest.mock import patch

import pytest

import app.backend.services.devils_advocate_service._technical_exhaustion as te
from app.backend.services.devils_advocate_service._schemas import Severity
from app.backend.services.pricing_service import TechnicalSnapshot


def _snap(pct_above, rsi):
    return TechnicalSnapshot(
        ticker="TEST", latest_close=150.0, sma200=100.0,
        pct_above_sma=pct_above, rsi14=rsi,
    )


@pytest.mark.asyncio
async def test_critical_when_stretched_and_overbought():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(35.0, 82.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert len(out) == 1
    assert out[0].severity == Severity.CRITICAL
    assert out[0].score == 60.0
    assert out[0].detector == "technical_exhaustion"


@pytest.mark.asyncio
async def test_warning_when_only_stretched():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(35.0, 60.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out[0].severity == Severity.WARNING
    assert out[0].score == 40.0


@pytest.mark.asyncio
async def test_warning_when_only_overbought():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(10.0, 80.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out[0].severity == Severity.WARNING


@pytest.mark.asyncio
async def test_info_band():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(25.0, 60.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out[0].severity == Severity.INFO
    assert out[0].score == 20.0


@pytest.mark.asyncio
async def test_no_finding_when_calm():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(5.0, 55.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out == []


@pytest.mark.asyncio
async def test_no_snapshot_returns_empty():
    with patch.object(te, "get_technical_snapshot", return_value=None):
        out = await te.detect_technical_exhaustion("TEST")
    assert out == []


@pytest.mark.asyncio
async def test_rsi_none_treated_as_not_overbought():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(10.0, None)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out == []  # ext below 20 and no RSI signal
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/backend/services/devils_advocate/test_technical_exhaustion.py -v`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Implement the detector**

```python
# app/backend/services/devils_advocate_service/_technical_exhaustion.py
"""Detector: technical exhaustion (mean-reversion risk).

Flags names stretched far above their 200-day trend and/or overbought on
RSI(14). High bullish conviction + stretched technicals = a common trap
where the thesis is right but the entry timing is late. Overlay-only: does
NOT alter the Discovery score.
"""
import logging

from app.backend.services.devils_advocate_service._schemas import RedFlagFinding, Severity
from app.backend.services.pricing_service import get_technical_snapshot

logger = logging.getLogger(__name__)

_EXT_STRETCHED = 30.0   # % above 200d SMA
_EXT_INFO = 20.0
_RSI_OVERBOUGHT = 75.0


async def detect_technical_exhaustion(ticker: str) -> list[RedFlagFinding]:
    """At most one finding. Empty on missing snapshot or calm technicals."""
    sym = ticker.strip().upper()
    snap = await get_technical_snapshot(sym)
    if snap is None:
        return []

    ext = snap.pct_above_sma
    rsi = snap.rsi14
    overbought = rsi is not None and rsi > _RSI_OVERBOUGHT
    stretched = ext > _EXT_STRETCHED

    if stretched and overbought:
        severity, score = Severity.CRITICAL, 60.0
    elif stretched or overbought:
        severity, score = Severity.WARNING, 40.0
    elif ext > _EXT_INFO:
        severity, score = Severity.INFO, 20.0
    else:
        return []

    rsi_txt = f"RSI {rsi:.0f}" if rsi is not None else "RSI n/a"
    headline = f"Technical exhaustion: +{ext:.0f}% above 200d trend, {rsi_txt} - mean-reversion risk"

    return [RedFlagFinding(
        detector="technical_exhaustion",
        score=score,
        severity=severity,
        headline=headline,
        detail={
            "ticker": sym,
            "pct_above_sma200": round(ext, 2),
            "rsi14": rsi,
            "latest_close": snap.latest_close,
            "sma200": round(snap.sma200, 2),
        },
    )]
```

- [ ] **Step 4: Run to verify it passes**

Run: `poetry run pytest tests/backend/services/devils_advocate/test_technical_exhaustion.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/backend/services/devils_advocate_service/_technical_exhaustion.py tests/backend/services/devils_advocate/test_technical_exhaustion.py
git commit -m "feat(devils-advocate): add technical_exhaustion detector"
```

---

### Task 4: exhausted_analyst detector + wire both into get_red_flags

**Files:**
- Create: `app/backend/services/devils_advocate_service/_exhausted_analyst.py`
- Modify: `app/backend/services/devils_advocate_service/__init__.py`
- Test: `tests/backend/services/devils_advocate/test_exhausted_analyst.py`

**Interfaces:**
- Consumes: `fundamentals_service.get_company_metrics_batch` (returns `dict[str, CompanyMetrics | None]`, each with `.target_mean_price` from Task 2), `pricing_service.get_technical_snapshot` for current price (`latest_close`), `RedFlagFinding`, `Severity`.
- Produces: `async def detect_exhausted_analyst(ticker: str) -> list[RedFlagFinding]`; both new detectors added to the `asyncio.gather(...)` in `get_red_flags`.

**Thresholds:** price = latest close from snapshot; target = `target_mean_price`. WARNING(40) when price >= 1.15*target; INFO(20) when target <= price < 1.15*target; else no finding. Missing target or snapshot -> no finding.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backend/services/devils_advocate/test_exhausted_analyst.py
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

import app.backend.services.devils_advocate_service._exhausted_analyst as ea
from app.backend.services.devils_advocate_service._schemas import Severity
from app.backend.services.pricing_service import TechnicalSnapshot


@dataclass
class _FakeMetrics:
    target_mean_price: float | None


def _snap(close):
    return TechnicalSnapshot("TEST", close, 100.0, 0.0, 50.0)


def _batch_mock(target):
    return AsyncMock(return_value={"TEST": _FakeMetrics(target_mean_price=target)})


@pytest.mark.asyncio
async def test_warning_far_above_target():
    with patch.object(ea, "get_technical_snapshot", return_value=_snap(130.0)), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(100.0)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out[0].severity == Severity.WARNING
    assert out[0].score == 40.0


@pytest.mark.asyncio
async def test_info_just_above_target():
    with patch.object(ea, "get_technical_snapshot", return_value=_snap(105.0)), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(100.0)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out[0].severity == Severity.INFO
    assert out[0].score == 20.0


@pytest.mark.asyncio
async def test_no_finding_below_target():
    with patch.object(ea, "get_technical_snapshot", return_value=_snap(80.0)), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(100.0)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out == []


@pytest.mark.asyncio
async def test_no_finding_missing_target():
    with patch.object(ea, "get_technical_snapshot", return_value=_snap(130.0)), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(None)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out == []


@pytest.mark.asyncio
async def test_no_finding_missing_snapshot():
    with patch.object(ea, "get_technical_snapshot", return_value=None), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(100.0)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/backend/services/devils_advocate/test_exhausted_analyst.py -v`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Implement the detector**

```python
# app/backend/services/devils_advocate_service/_exhausted_analyst.py
"""Detector: price trading above the mean analyst price target.

The buildable proxy for a "chased upgrade" — when price already exceeds the
consensus target, further upside is limited and recent upgrades may reflect
momentum rather than fundamental headroom. No NLP (report text is paywalled);
uses the numeric target only. Overlay-only: does NOT alter the Discovery score.
"""
import logging

from app.backend.services.devils_advocate_service._schemas import RedFlagFinding, Severity
from app.backend.services.fundamentals_service import get_company_metrics_batch
from app.backend.services.pricing_service import get_technical_snapshot

logger = logging.getLogger(__name__)

_STRETCH_MULT = 1.15  # price >= 1.15x target -> WARNING


async def detect_exhausted_analyst(ticker: str) -> list[RedFlagFinding]:
    """At most one finding. Empty on missing target, missing price, or price
    below target.
    """
    sym = ticker.strip().upper()
    snap = await get_technical_snapshot(sym)
    if snap is None:
        return []
    price = snap.latest_close

    metrics_by_ticker = await get_company_metrics_batch([sym])
    m = metrics_by_ticker.get(sym)
    target = getattr(m, "target_mean_price", None) if m is not None else None
    if target is None or target <= 0:
        return []
    if price < target:
        return []

    if price >= _STRETCH_MULT * target:
        severity, score = Severity.WARNING, 40.0
    else:
        severity, score = Severity.INFO, 20.0

    headline = (
        f"Above analyst target: trading at ${price:.2f} vs ${target:.2f} mean target "
        "- limited upside / chased momentum"
    )
    return [RedFlagFinding(
        detector="exhausted_analyst",
        score=score,
        severity=severity,
        headline=headline,
        detail={
            "ticker": sym,
            "latest_close": price,
            "target_mean_price": target,
            "price_to_target": round(price / target, 3),
        },
    )]
```

- [ ] **Step 4: Wire both detectors into get_red_flags**

In `app/backend/services/devils_advocate_service/__init__.py`, add imports beside the existing detector imports:

```python
from app.backend.services.devils_advocate_service._technical_exhaustion import (
    detect_technical_exhaustion,
)
from app.backend.services.devils_advocate_service._exhausted_analyst import (
    detect_exhausted_analyst,
)
```

Extend the `asyncio.gather(...)` call in `get_red_flags` to include the two new detectors:

```python
    detector_results = await asyncio.gather(
        detect_ceo_cfo_divergence(sym),
        detect_altman_z_score(sym),
        detect_beneish_m_score(sym),
        detect_technical_exhaustion(sym),
        detect_exhausted_analyst(sym),
        return_exceptions=True,
    )
```

- [ ] **Step 5: Run detector tests + a wiring smoke test**

Run: `poetry run pytest tests/backend/services/devils_advocate/test_exhausted_analyst.py -v`
Expected: all PASS

Then verify the wiring imports cleanly:

Run: `poetry run python -c "from app.backend.services.devils_advocate_service import get_red_flags; print('ok')"`
Expected: prints `ok` (ignore the urllib3 warning line)

- [ ] **Step 6: Commit**

```bash
git add app/backend/services/devils_advocate_service/_exhausted_analyst.py app/backend/services/devils_advocate_service/__init__.py tests/backend/services/devils_advocate/test_exhausted_analyst.py
git commit -m "feat(devils-advocate): add exhausted_analyst detector, wire both into get_red_flags"
```

---

### Task 5: sector_rotation_divergence alert rule

**Files:**
- Create: `app/backend/services/alert_service/_rules/sector_rotation_rule.py`
- Modify: `app/backend/services/alert_service/_rules/__init__.py`
- Test: `tests/backend/services/alert_service/test_sector_rotation_rule.py`

**Interfaces:**
- Consumes: `pricing_service.compute_alpha` for 3-trading-day returns of SMH and IWM (returns an object with `.period_return_pct`, or None); `AlertCandidate` from `alert_service._types`; existing RULES registry.
- Produces: `async def evaluate(thresholds: dict) -> list[AlertCandidate]`; registry entry `("sector_rotation", sector_rotation_rule.evaluate, _sector_rotation_thresholds)`.

**Trigger (from spec):** `SMH_3d_return_pct < -2.0` AND `IWM_3d_return_pct > +1.0`. Fixed synthetic ticker `"_MARKET_"`. Thresholds configurable: `smh_max_pct` (default -2.0), `iwm_min_pct` (default 1.0), `lookback_days` (default 3). The rule is stateless — it emits a candidate whenever the condition holds; the alert sink de-dupes on `(rule_type, ticker, created_at)` so a multi-day episode surfaces once per scan window.

**PRE-STEP (read first):** Open `app/backend/services/alert_service/_types.py` and confirm `AlertCandidate` field names (`rule_type`, `ticker`, `title`, `message`, `payload`, `severity`). These match `technical_breakdown.py`'s usage. If any field name differs, align the constructor call in Step 3.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backend/services/alert_service/test_sector_rotation_rule.py
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

import app.backend.services.alert_service._rules.sector_rotation_rule as rot


@dataclass
class _Ret:
    period_return_pct: float


def _thresholds():
    return {"smh_max_pct": -2.0, "iwm_min_pct": 1.0, "lookback_days": 3}


def _compute_mock(mapping):
    async def _compute(ticker, since):
        val = mapping.get(ticker.upper())
        return _Ret(val) if val is not None else None
    return _compute


@pytest.mark.asyncio
async def test_fires_on_rotation():
    with patch.object(rot, "compute_alpha", _compute_mock({"SMH": -2.4, "IWM": 1.8})):
        out = await rot.evaluate(_thresholds())
    assert len(out) == 1
    assert out[0].rule_type == "sector_rotation"
    assert out[0].ticker == "_MARKET_"
    assert out[0].severity in ("warning", "critical")


@pytest.mark.asyncio
async def test_no_fire_when_smh_only_flat():
    with patch.object(rot, "compute_alpha", _compute_mock({"SMH": -0.5, "IWM": 1.8})):
        out = await rot.evaluate(_thresholds())
    assert out == []


@pytest.mark.asyncio
async def test_no_fire_when_iwm_not_up():
    with patch.object(rot, "compute_alpha", _compute_mock({"SMH": -3.0, "IWM": -0.2})):
        out = await rot.evaluate(_thresholds())
    assert out == []


@pytest.mark.asyncio
async def test_no_fire_when_pricing_unavailable():
    with patch.object(rot, "compute_alpha", _compute_mock({})):  # both None
        out = await rot.evaluate(_thresholds())
    assert out == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `poetry run pytest tests/backend/services/alert_service/test_sector_rotation_rule.py -v`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Implement the rule**

```python
# app/backend/services/alert_service/_rules/sector_rotation_rule.py
"""sector_rotation_divergence alert rule (market-wide).

Detects money rotating OUT of semiconductors and INTO small caps: the
Semiconductor ETF (SMH) falling while the small-cap ETF (IWM) rises over the
same short window. This is the macro "tide" that drags high-beta semi
holdings down regardless of company fundamentals.

Market-wide, so it has no single ticker — emits under the synthetic ticker
"_MARKET_". The alert sink de-dupes on (rule_type, ticker, created_at), so a
multi-day episode surfaces once per scan window rather than per ticker.
"""
import logging
from datetime import date, timedelta

from app.backend.services.alert_service._types import AlertCandidate
from app.backend.services.pricing_service import compute_alpha

logger = logging.getLogger(__name__)

_MARKET_TICKER = "_MARKET_"
_SEMI_ETF = "SMH"
_SMALLCAP_ETF = "IWM"


async def evaluate(thresholds: dict) -> list[AlertCandidate]:
    smh_max = float(thresholds.get("smh_max_pct", -2.0))
    iwm_min = float(thresholds.get("iwm_min_pct", 1.0))
    lookback = int(thresholds.get("lookback_days", 3))
    since = date.today() - timedelta(days=lookback)

    smh = await compute_alpha(_SEMI_ETF, since)
    iwm = await compute_alpha(_SMALLCAP_ETF, since)
    if smh is None or iwm is None:
        logger.debug("sector_rotation: pricing unavailable, skipping scan")
        return []

    smh_ret = smh.period_return_pct
    iwm_ret = iwm.period_return_pct
    if not (smh_ret < smh_max and iwm_ret > iwm_min):
        return []

    severity = "critical" if smh_ret <= (smh_max - 1.5) else "warning"
    title = f"Sector rotation: semis {smh_ret:.1f}% vs small-caps +{iwm_ret:.1f}%"
    message = (
        f"Money is rotating out of semiconductors and into small caps over the last "
        f"{lookback} trading days.\n"
        f"SMH (semis): {smh_ret:.1f}%\n"
        f"IWM (small caps): +{iwm_ret:.1f}%\n"
        "High-beta semiconductor holdings are at elevated pullback risk - consider "
        "tightening stops."
    )
    return [AlertCandidate(
        rule_type="sector_rotation",
        ticker=_MARKET_TICKER,
        title=title,
        message=message,
        payload={
            "smh_return_pct": round(smh_ret, 2),
            "iwm_return_pct": round(iwm_ret, 2),
            "lookback_days": lookback,
        },
        severity=severity,
    )]
```

- [ ] **Step 4: Run to verify it passes**

Run: `poetry run pytest tests/backend/services/alert_service/test_sector_rotation_rule.py -v`
Expected: all PASS

- [ ] **Step 5: Register the rule**

In `app/backend/services/alert_service/_rules/__init__.py`:

Add to the `from . import (...)` block (keep alphabetical order):

```python
    sector_rotation_rule,
```

Add a thresholds extractor near the others:

```python
def _sector_rotation_thresholds(settings: dict) -> dict:
    return {
        "smh_max_pct": float(settings.get("sector_rotation_smh_max_pct", -2.0)),
        "iwm_min_pct": float(settings.get("sector_rotation_iwm_min_pct", 1.0)),
        "lookback_days": int(settings.get("sector_rotation_lookback_days", 3)),
    }
```

Add to the `RULES` list:

```python
    ("sector_rotation", sector_rotation_rule.evaluate, _sector_rotation_thresholds),
```

- [ ] **Step 6: Verify registry imports + full suite green**

Run: `poetry run python -c "from app.backend.services.alert_service._rules import RULES; print([r[0] for r in RULES])"`
Expected: list includes `'sector_rotation'`

Run: `poetry run pytest tests/backend/services/ -q`
Expected: all new tests PASS, no regressions.

- [ ] **Step 7: Commit**

```bash
git add app/backend/services/alert_service/_rules/sector_rotation_rule.py app/backend/services/alert_service/_rules/__init__.py tests/backend/services/alert_service/test_sector_rotation_rule.py
git commit -m "feat(alerts): add sector_rotation_divergence market-wide rule"
```

---

## Notes for the implementer

- **GateGuard:** this session's fact-forcing gate fires on the first create/edit of each file. If `ECC_GATEGUARD=off` is set (requires a fresh `claude` session), it's bypassed. Otherwise, state importer + no-duplicate + data-shape + the verbatim goal before each first file write.
- **Frontend:** none needed. The Devil's Advocate badge column already renders any `RedFlagFinding`, and the alert feed already renders any `AlertCandidate`. New findings/alerts appear automatically once the Devil's Advocate toggle is on.
- **Migration:** none — no new tables.
- **`pytest-asyncio`:** tests use `@pytest.mark.asyncio`. Confirm the project's pytest config enables asyncio mode (existing async detector tests already rely on it — mirror their pattern, e.g. `_ceo_cfo_divergence` tests).
- **Verify overlay isolation after Task 4:** confirm no file under `discovery_service/` imports `devils_advocate_service` — `grep -rn "devils_advocate" app/backend/services/discovery_service/` must return nothing. This is the guardrail for the standing constraint.
