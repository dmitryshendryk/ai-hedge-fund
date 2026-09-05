"""Devil's Advocate routes — per-ticker bear-thesis overlay + toggle.

Importers/callers: registered by app/backend/routes/__init__.py.
Affected API: NEW endpoints under /devils_advocate prefix:
  GET  /devils_advocate/red_flags/{ticker} -> RedFlagReport
  GET  /devils_advocate/settings           -> {enabled: bool}
  PUT  /devils_advocate/settings           -> {enabled: bool}
No Discovery routes touched. Discovery state is not read or written.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.backend.services.devils_advocate_service import (
    RedFlagReport,
    get_red_flags,
    is_enabled,
    set_enabled,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devils_advocate", tags=["devils-advocate"])


class DevilsAdvocateSettingResponse(BaseModel):
    enabled: bool


class DevilsAdvocateSettingRequest(BaseModel):
    enabled: bool


@router.get("/settings", response_model=DevilsAdvocateSettingResponse)
def get_settings() -> DevilsAdvocateSettingResponse:
    return DevilsAdvocateSettingResponse(enabled=is_enabled())


@router.put("/settings", response_model=DevilsAdvocateSettingResponse)
def put_settings(request: DevilsAdvocateSettingRequest) -> DevilsAdvocateSettingResponse:
    new_state = set_enabled(request.enabled)
    return DevilsAdvocateSettingResponse(enabled=new_state)


@router.get("/red_flags/{ticker}", response_model=RedFlagReport)
async def get_red_flags_for(ticker: str) -> RedFlagReport:
    sym = ticker.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="ticker is required")
    return await get_red_flags(sym)
