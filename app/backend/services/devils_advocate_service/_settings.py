"""Persistent on/off toggle for the Devil's Advocate overlay.

Stored in the existing `app_settings` key/value table (see settings route
for the LLM model precedent). Default is OFF so the feature is opt-in
and the existing Discovery experience is unchanged for users who don't
turn it on.
"""
import logging

from app.backend.database import SessionLocal
from app.backend.database.models import AppSetting

logger = logging.getLogger(__name__)

_KEY = "devils_advocate_enabled"
_DEFAULT = False


def is_enabled() -> bool:
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == _KEY).first()
    finally:
        db.close()
    if row is None:
        return _DEFAULT
    return row.value.strip().lower() in {"1", "true", "yes", "on"}


def set_enabled(enabled: bool) -> bool:
    value = "true" if enabled else "false"
    db = SessionLocal()
    try:
        row = db.query(AppSetting).filter(AppSetting.key == _KEY).first()
        if row is None:
            db.add(AppSetting(key=_KEY, value=value))
        else:
            row.value = value
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return enabled
