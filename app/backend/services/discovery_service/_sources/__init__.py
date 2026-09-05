"""Discovery source registry. Add a new source by appending to SOURCES."""

from collections.abc import Awaitable, Callable

from . import (
    activist_13d,
    analyst,
    cluster_buy,
    commodity_tailwind,
    contrarian_setup,
    csuite_buy,
    dividend_grower,
    fcf_yield,
    first_time_buyer,
    gov_contract_win,
    high_roic,
    hiring_velocity,
    insider_doubling_down,
    mega_dollar_buy,
    piotroski_score,
    quality_score,
    relative_strength,
    repeat_buyer,
    revenue_acceleration,
    share_cannibal,
    spinoff,
    squeeze,
    thirteenf_new_buy,
    true_shareholder_yield,
    valuation_score,
    vcp_breakout_setup,
)
from app.backend.models.discovery_schemas import IdeaSignal

# Each source: async () -> list[(key, IdeaSignal)]
# `key` = ticker symbol, OR "cik:N" for entities without a public ticker yet.
SourceFn = Callable[[], Awaitable[list[tuple[str, IdeaSignal]]]]

SOURCES: list[tuple[str, SourceFn]] = [
    ("spinoff", spinoff.fetch),
    ("csuite_buy", csuite_buy.fetch),
    ("squeeze", squeeze.fetch),
    ("cluster_buy", cluster_buy.fetch),
    ("analyst", analyst.fetch),
    ("commodity_tailwind", commodity_tailwind.fetch),
    ("insider_doubling_down", insider_doubling_down.fetch),
    ("first_time_buyer", first_time_buyer.fetch),
    ("mega_dollar_buy", mega_dollar_buy.fetch),
    ("repeat_buyer", repeat_buyer.fetch),
    ("relative_strength", relative_strength.fetch),
    ("contrarian_setup", contrarian_setup.fetch),
    ("activist_13d", activist_13d.fetch),
    ("revenue_acceleration", revenue_acceleration.fetch),
    ("quality_score", quality_score.fetch),
    ("valuation_score", valuation_score.fetch),
    ("dividend_grower", dividend_grower.fetch),
    ("fcf_yield", fcf_yield.fetch),
    ("high_roic", high_roic.fetch),
    ("gov_contract_win", gov_contract_win.fetch),
    ("hiring_velocity", hiring_velocity.fetch),
    ("share_cannibal", share_cannibal.fetch),
    ("thirteenf_new_buy", thirteenf_new_buy.fetch),
    ("vcp_breakout_setup", vcp_breakout_setup.fetch),
    ("true_shareholder_yield", true_shareholder_yield.fetch),
    ("piotroski_score", piotroski_score.fetch),
]

# kronos_predictive_surge is deliberately unregistered, and no Devil's Advocate
# detector reads Kronos either. Measured prob_up clusters at 0.03/0.97 rather
# than spreading like a probability, and the forecast sign flips with the
# lookback window. tools/kronos_backtest.py is what would justify enabling it.
