"""Measures whether Kronos prob_up predicts realised returns.

Walk-forward: at each evaluation bar the model sees only prior bars, and its
forecast is scored against the return that actually followed. Answers the one
question that decides whether kronos_predictive_surge should score in Discovery —
does a high prob_up beat simply holding?

    poetry run python tools/kronos_backtest.py --tickers AAPL,MSFT,NVDA

Inference dominates the runtime, so it prints a cost estimate then streams
progress as each ticker and lookback lands.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kronos_forecast import (  # noqa: E402  (sys.path must be set before this import)
    _PRICE_COLS,
    _build_predictor,
    _parse_args as _forecast_args,
    _sampled_paths,
)

logger = logging.getLogger("kronos_backtest")

_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True, kw_only=True)
class Evaluation:
    """One forecast scored against what actually happened next."""

    ticker: str
    as_of: str
    lookback: int
    prob_up: float
    expected_return_pct: float
    realised_return_pct: float


def _fetch_daily(ticker: str, bars_needed: int) -> pd.DataFrame | None:
    """Full daily OHLCV history, shaped as Kronos wants it."""
    import yfinance as yf

    period_days = int(bars_needed * 1.8) + 60
    try:
        raw = yf.Ticker(ticker).history(period=f"{period_days}d", auto_adjust=True)
    except Exception as exc:
        logger.warning("%s: history fetch failed: %s", ticker, exc)
        return None
    if raw is None or raw.empty:
        return None

    frame = raw.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if frame.empty:
        return None
    out = pd.DataFrame({
        "open": frame["Open"].astype(float),
        "high": frame["High"].astype(float),
        "low": frame["Low"].astype(float),
        "close": frame["Close"].astype(float),
        "volume": frame["Volume"].astype(float),
    })
    out["amount"] = out["close"] * out["volume"]
    out.index = pd.to_datetime(frame.index).tz_localize(None)
    return out


def _evaluate(
    predictor,
    history: pd.DataFrame,
    ticker: str,
    lookback: int,
    horizon: int,
    dates: int,
    sample_count: int,
) -> list[Evaluation]:
    """Score `dates` forecasts spread across the usable span of one ticker."""
    settings = _forecast_args([
        "--tickers", ticker,
        "--sample-count", str(sample_count),
        "--horizons", str(horizon),
        "--lookback", str(lookback),
    ])

    # The window is history[end-lookback:end]; the realised move runs from end-1
    # to end-1+horizon, so `end` may not exceed len - horizon. Nothing at or
    # after `end` is ever shown to the model.
    first_end = lookback
    last_end = len(history) - horizon
    if last_end <= first_end:
        return []
    count = min(dates, last_end - first_end + 1)
    ends = np.unique(np.linspace(first_end, last_end, num=count, dtype=int))

    out: list[Evaluation] = []
    for end in ends:
        window = history.iloc[end - lookback:end]
        last_close = float(window["close"].iloc[-1])
        realised_close = float(history["close"].iloc[end - 1 + horizon])
        if last_close <= 0 or realised_close <= 0:
            continue

        paths = _sampled_paths(predictor, window[_PRICE_COLS], settings)
        if paths is None or paths.shape[1] < horizon:
            continue
        at_horizon = paths[:, horizon - 1]

        out.append(Evaluation(
            ticker=ticker,
            as_of=window.index[-1].strftime("%Y-%m-%d"),
            lookback=lookback,
            prob_up=float(np.mean(at_horizon > last_close)),
            expected_return_pct=float(np.mean((at_horizon / last_close - 1.0) * 100.0)),
            realised_return_pct=(realised_close / last_close - 1.0) * 100.0,
        ))
    return out


def _spearman(x: list[float], y: list[float]) -> float | None:
    """Rank correlation, hand-rolled to avoid a scipy dependency."""
    if len(x) < 3:
        return None
    rx = pd.Series(x).rank().to_numpy()
    ry = pd.Series(y).rank().to_numpy()
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def _score(evaluations: list[Evaluation], thresholds: list[float]) -> list[dict]:
    """Per-lookback, per-threshold edge over the base rate of just holding."""
    rows: list[dict] = []
    for lookback in sorted({e.lookback for e in evaluations}):
        group = [e for e in evaluations if e.lookback == lookback]
        realised = [e.realised_return_pct for e in group]
        base_rate = float(np.mean(realised))
        rows.append({
            "lookback": lookback,
            "threshold": None,
            "n": len(group),
            "hit_rate": float(np.mean([r > 0 for r in realised])),
            "mean_return_when_signalled": base_rate,
            "base_rate_mean_return": base_rate,
            "edge_pct": 0.0,
            "spearman_prob_up_vs_realised": _spearman([e.prob_up for e in group], realised),
        })
        for threshold in thresholds:
            signalled = [e.realised_return_pct for e in group if e.prob_up >= threshold]
            if not signalled:
                rows.append({
                    "lookback": lookback, "threshold": threshold, "n": 0,
                    "hit_rate": None, "mean_return_when_signalled": None,
                    "base_rate_mean_return": base_rate, "edge_pct": None,
                    "spearman_prob_up_vs_realised": None,
                })
                continue
            mean_signalled = float(np.mean(signalled))
            rows.append({
                "lookback": lookback,
                "threshold": threshold,
                "n": len(signalled),
                "hit_rate": float(np.mean([r > 0 for r in signalled])),
                "mean_return_when_signalled": mean_signalled,
                "base_rate_mean_return": base_rate,
                "edge_pct": mean_signalled - base_rate,
                "spearman_prob_up_vs_realised": None,
            })
    return rows


def _print_table(rows: list[dict]) -> None:
    print(f"\n{'lookback':>8} {'thresh':>7} {'n':>5} {'hit%':>6} {'mean%':>8} {'base%':>8} {'edge%':>8}  note")
    for row in rows:
        threshold = "hold" if row["threshold"] is None else f"{row['threshold']:.2f}"
        if row["n"] == 0:
            print(f"{row['lookback']:>8} {threshold:>7} {0:>5} {'-':>6} {'-':>8} "
                  f"{row['base_rate_mean_return']:>8.2f} {'-':>8}  never fired")
            continue
        note = ""
        if row["threshold"] is None and row["spearman_prob_up_vs_realised"] is not None:
            note = f"spearman(prob_up, realised) = {row['spearman_prob_up_vs_realised']:+.3f}"
        print(
            f"{row['lookback']:>8} {threshold:>7} {row['n']:>5} {row['hit_rate'] * 100:>6.1f} "
            f"{row['mean_return_when_signalled']:>8.2f} {row['base_rate_mean_return']:>8.2f} "
            f"{row['edge_pct']:>8.2f}  {note}"
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Backtest Kronos prob_up against realised returns.")
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--lookbacks", default="120,250,400")
    parser.add_argument("--horizon", type=int, default=5, help="Trading days ahead, matching the surge signal")
    parser.add_argument("--dates", type=int, default=24, help="Evaluation points per ticker per lookback")
    parser.add_argument("--sample-count", type=int, default=64)
    parser.add_argument("--thresholds", default="0.60,0.70,0.80,0.90")
    parser.add_argument("--device", default=None)
    parser.add_argument("--out", default=str(_REPO_ROOT / ".out/kronos_backtest.json"))
    args = parser.parse_args(argv)

    tickers = sorted({t.strip().upper() for t in args.tickers.split(",") if t.strip()})
    lookbacks = sorted({int(v) for v in args.lookbacks.split(",") if v.strip()})
    thresholds = sorted({float(v) for v in args.thresholds.split(",") if v.strip()})

    runs = len(tickers) * len(lookbacks) * args.dates
    print(f"{runs} inferences ({len(tickers)} tickers x {len(lookbacks)} lookbacks x {args.dates} dates).")
    print(f"At ~5s each on MPS with sample_count={args.sample_count}, expect roughly {runs * 5 / 60:.0f} min.\n")

    predictor_args = ["--tickers", "X"] + (["--device", args.device] if args.device else [])
    predictor = _build_predictor(_forecast_args(predictor_args))

    evaluations: list[Evaluation] = []
    started = time.perf_counter()
    for ticker in tickers:
        history = _fetch_daily(ticker, max(lookbacks) + args.horizon + args.dates + 20)
        if history is None:
            continue
        for lookback in lookbacks:
            batch = _evaluate(predictor, history, ticker, lookback, args.horizon, args.dates, args.sample_count)
            evaluations.extend(batch)
            logger.info(
                "%s lookback=%d: %d evaluations (%.0fs elapsed)",
                ticker, lookback, len(batch), time.perf_counter() - started,
            )

    if not evaluations:
        logger.error("no evaluations produced; check ticker symbols and network access")
        return 1

    rows = _score(evaluations, thresholds)
    _print_table(rows)
    print("\nedge% is the mean realised return when the signal fired, minus the return from")
    print("holding over those same dates. A signal worth scoring shows a positive edge that")
    print("holds across lookbacks, on an n large enough to mean something.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "tickers": tickers, "lookbacks": lookbacks, "horizon": args.horizon,
            "dates": args.dates, "sample_count": args.sample_count, "thresholds": thresholds,
        },
        "results": rows,
        "evaluations": [vars(e) for e in evaluations],
    }, indent=2))
    print(f"\nwrote {len(evaluations)} evaluations to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
