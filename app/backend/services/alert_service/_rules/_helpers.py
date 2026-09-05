"""Shared helpers used by multiple alert rules."""

from app.backend.database import SessionLocal
from app.backend.database.models import WatchlistItem


def watchlist_ticker_set() -> set[str]:
    """Uppercased tickers on the user's watchlist.

    Exit rules scope by watchlist so unrelated tickers don't generate noise.
    """
    db = SessionLocal()
    try:
        return {
            row[0].upper()
            for row in db.query(WatchlistItem.ticker).all()
            if row[0]
        }
    finally:
        db.close()
