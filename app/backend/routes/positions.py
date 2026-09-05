"""Position (holdings) routes — CRUD + live P&L list."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.backend.database import get_db
from app.backend.models.position_schemas import (
    ConcentrationPreviewResponse,
    PositionAddRequest,
    PositionListResponse,
    PositionResponse,
    PositionUpdateRequest,
)
from app.backend.services.position_service import (
    add_position,
    add_position_with_stop,
    list_positions_enriched,
    preview_concentration,
    remove_position,
    update_position,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("/", response_model=PositionListResponse)
async def list_endpoint(db: Session = Depends(get_db)) -> PositionListResponse:
    return await list_positions_enriched(db)


# Declared above the /{ticker} routes so "concentration" is never captured as
# a ticker path parameter.
@router.get("/concentration/preview", response_model=ConcentrationPreviewResponse)
async def preview_endpoint(
    ticker: str,
    amount: float,
    db: Session = Depends(get_db),
) -> ConcentrationPreviewResponse:
    try:
        return await preview_concentration(db, ticker, amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/", response_model=PositionResponse)
async def add_endpoint(req: PositionAddRequest, db: Session = Depends(get_db)) -> PositionResponse:
    try:
        return await add_position_with_stop(db, req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{ticker}", response_model=PositionResponse)
def update_endpoint(ticker: str, req: PositionUpdateRequest, db: Session = Depends(get_db)) -> PositionResponse:
    try:
        item = update_position(db, ticker, req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail=f"{ticker} not in positions")
    return item


@router.delete("/{ticker}", response_model=dict)
def delete_endpoint(ticker: str, db: Session = Depends(get_db)) -> dict:
    if not remove_position(db, ticker):
        raise HTTPException(status_code=404, detail=f"{ticker} not in positions")
    return {"ok": True}
