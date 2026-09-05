"""Writes Kronos forecasts to a JSON artifact the Discovery pipeline reads.

Run from cron or by hand; nothing in the API process calls it. The vendored
Kronos/ directory is on no import path by default, so this module adds it.

    poetry run python tools/kronos_forecast.py --tickers AAPL,MSFT,NVDA
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger("kronos_forecast")

SCHEMA_VERSION = 1

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENDORED_KRONOS = _REPO_ROOT / "Kronos"

_PRICE_COLS = ["open", "high", "low", "close", "volume", "amount"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    tickers: list[str]
    horizons: list[int]
    lookback: int
    pred_len: int
    sample_count: int
    seed: int
    temperature: float
    top_p: float
    device: str | None
    model_name: str
    tokenizer_name: str
    max_context: int
    out_path: Path
    kronos_path: Path


def _parse_args(argv: list[str] | None = None) -> Settings:
    parser = argparse.ArgumentParser(description="Write Kronos forecasts for the Discovery pipeline.")
    parser.add_argument("--tickers", required=True, help="Comma-separated symbols, e.g. AAPL,MSFT")
    parser.add_argument("--horizons", default="5,7", help="Trading-day horizons to summarise")
    parser.add_argument("--lookback", type=int, default=400, help="Daily bars of history fed to the model")
    parser.add_argument("--sample-count", type=int, default=128,
                        help="Sampled paths per ticker; prob_up's standard error is ~sqrt(0.25/N)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Sampling is stochastic and Kronos sets no seed, so this makes a run reproducible")
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--device", default=None, help="cpu, mps, or cuda:0. Auto-detected when omitted")
    parser.add_argument("--model", default="NeoQuasar/Kronos-small")
    parser.add_argument("--tokenizer", default="NeoQuasar/Kronos-Tokenizer-base")
    parser.add_argument("--max-context", type=int, default=512)
    parser.add_argument("--out",
                        default=os.environ.get("KRONOS_FORECAST_PATH") or str(_REPO_ROOT / ".out/kronos_forecasts.json"))
    parser.add_argument("--kronos-path", default=os.environ.get("KRONOS_REPO_PATH") or str(_VENDORED_KRONOS),
                        help="Kronos checkout supplying the `model` package")
    args = parser.parse_args(argv)

    horizons = sorted({int(h) for h in args.horizons.split(",") if h.strip()})
    if not horizons:
        parser.error("--horizons needs at least one integer")
    tickers = sorted({t.strip().upper() for t in args.tickers.split(",") if t.strip()})
    if not tickers:
        parser.error("--tickers needs at least one symbol")
    if args.sample_count < 1:
        parser.error("--sample-count must be at least 1")

    return Settings(
        tickers=tickers,
        horizons=horizons,
        lookback=args.lookback,
        # Forecast far enough to cover the longest horizon and no further.
        pred_len=max(horizons),
        sample_count=args.sample_count,
        seed=args.seed,
        temperature=args.temperature,
        top_p=args.top_p,
        device=args.device,
        model_name=args.model,
        tokenizer_name=args.tokenizer,
        max_context=args.max_context,
        out_path=Path(args.out),
        kronos_path=Path(args.kronos_path).expanduser().resolve(),
    )


def _load_history(ticker: str, lookback: int) -> pd.DataFrame | None:
    """Daily OHLCV shaped for Kronos, or None when yfinance returns too little.

    Kronos wants lowercase columns plus `amount`, which US feeds do not publish;
    close * volume is the standard stand-in for turnover.
    """
    import yfinance as yf

    # Ask for well over `lookback` calendar days to survive weekends and halts.
    period_days = int(lookback * 1.7) + 40
    try:
        raw = yf.Ticker(ticker).history(period=f"{period_days}d", auto_adjust=True)
    except Exception as exc:
        logger.warning("%s: history fetch failed: %s", ticker, exc)
        return None
    if raw is None or raw.empty:
        return None

    frame = raw.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(frame) < lookback:
        logger.warning("%s: only %d usable bars, need %d", ticker, len(frame), lookback)
        return None

    frame = frame.tail(lookback)
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


def _sampled_paths(predictor, history: pd.DataFrame, settings: Settings) -> np.ndarray | None:
    """Close prices per sampled path, shaped (sample_count, pred_len).

    KronosPredictor.predict averages its sample_count paths internally and
    returns only the mean, which discards the spread prob_up needs. Passing the
    same series sample_count times with sample_count=1 makes every batch row an
    independent path in one batched forward pass, so no fork of their sampler.
    """
    x_timestamp = pd.Series(history.index)
    future = pd.bdate_range(start=history.index[-1] + pd.Timedelta(days=1), periods=settings.pred_len)
    y_timestamp = pd.Series(future)

    n = settings.sample_count
    try:
        results = predictor.predict_batch(
            df_list=[history[_PRICE_COLS]] * n,
            x_timestamp_list=[x_timestamp] * n,
            y_timestamp_list=[y_timestamp] * n,
            pred_len=settings.pred_len,
            T=settings.temperature,
            top_p=settings.top_p,
            sample_count=1,
            verbose=False,
        )
    except Exception as exc:
        logger.warning("prediction failed: %s", exc)
        return None

    closes = [np.asarray(df["close"], dtype=float) for df in results if "close" in df]
    if len(closes) != n:
        logger.warning("expected %d paths, got %d", n, len(closes))
        return None
    return np.vstack(closes)


def _summarise(paths: np.ndarray, last_close: float, horizons: list[int]) -> dict[str, dict[str, float]]:
    """Per-horizon direction and return statistics over the sampled paths."""
    out: dict[str, dict[str, float]] = {}
    for days in horizons:
        if days < 1 or days > paths.shape[1]:
            continue
        ends = paths[:, days - 1]
        returns = (ends / last_close - 1.0) * 100.0
        out[str(days)] = {
            "prob_up": round(float(np.mean(ends > last_close)), 4),
            "expected_return_pct": round(float(np.mean(returns)), 4),
            "p10_return_pct": round(float(np.percentile(returns, 10)), 4),
            "p90_return_pct": round(float(np.percentile(returns, 90)), 4),
        }
    return out


def _write_atomic(path: Path, payload: dict) -> None:
    """Write via a sibling temp file so a reader never sees a partial artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def _build_predictor(settings: Settings):
    """Load the model onto the chosen device, seeding torch for reproducibility."""
    if not (settings.kronos_path / "model").is_dir():
        raise SystemExit(f"No Kronos `model` package under {settings.kronos_path}; pass --kronos-path")
    # Kronos does `from model.module import *`, so its root must be importable.
    sys.path.insert(0, str(settings.kronos_path))

    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer

    torch.manual_seed(settings.seed)

    tokenizer = KronosTokenizer.from_pretrained(settings.tokenizer_name)
    model = Kronos.from_pretrained(settings.model_name)
    predictor = KronosPredictor(model, tokenizer, device=settings.device, max_context=settings.max_context)

    # Kronos never leaves training mode, and its attention passes a non-zero
    # dropout_p while self.training holds. That crashes MPS, which rejects
    # dropout in scaled_dot_product_attention, and silently randomises
    # predictions everywhere else. eval() is what makes a forecast repeatable.
    predictor.model.eval()
    predictor.tokenizer.eval()
    return predictor


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = _parse_args(argv)
    predictor = _build_predictor(settings)
    logger.info("device=%s model=%s samples=%d", predictor.device, settings.model_name, settings.sample_count)

    forecasts: dict[str, dict] = {}
    for ticker in settings.tickers:
        history = _load_history(ticker, settings.lookback)
        if history is None:
            continue
        last_close = float(history["close"].iloc[-1])
        if last_close <= 0:
            continue

        paths = _sampled_paths(predictor, history, settings)
        if paths is None:
            continue
        horizons = _summarise(paths, last_close, settings.horizons)
        if not horizons:
            continue

        forecasts[ticker] = {"last_close": round(last_close, 4), "horizons": horizons}
        logger.info("%s: prob_up %s", ticker, {d: h["prob_up"] for d, h in horizons.items()})

    _write_atomic(settings.out_path, {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": settings.model_name,
        "params": {
            "lookback": settings.lookback,
            "pred_len": settings.pred_len,
            "sample_count": settings.sample_count,
            "seed": settings.seed,
            "temperature": settings.temperature,
            "top_p": settings.top_p,
        },
        "forecasts": forecasts,
    })
    logger.info("wrote %d forecast(s) to %s", len(forecasts), settings.out_path)
    return 0 if forecasts else 1


if __name__ == "__main__":
    raise SystemExit(main())
