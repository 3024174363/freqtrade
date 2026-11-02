from fastapi import APIRouter, Depends, HTTPException, Query, Request

from freqtrade.data.ashare.service import MarketUnavailable
from freqtrade.data.hkshare.models import CandlesResponse, QuoteResponse, SearchResponse, Timeframe
from freqtrade.data.hkshare.service import HKShareService


router = APIRouter(prefix="/hkshare", tags=["Hong Kong market data"])


def get_hkshare_service(request: Request) -> HKShareService:
    return request.app.state.hkshare_service


@router.get("/capabilities")
async def capabilities(service: HKShareService = Depends(get_hkshare_service)):
    return service.capabilities()


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(default="", max_length=80, pattern=r"^[^\x00-\x1f]*$"),
    service: HKShareService = Depends(get_hkshare_service),
):
    try:
        return await service.search(q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/quote", response_model=QuoteResponse)
async def quote(
    symbol: str = Query(max_length=16),
    service: HKShareService = Depends(get_hkshare_service),
):
    try:
        return await service.quote(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MarketUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/candles", response_model=CandlesResponse)
async def candles(
    symbol: str = Query(max_length=16),
    timeframe: Timeframe = "1d",
    limit: int = Query(default=320, ge=10, le=500),
    service: HKShareService = Depends(get_hkshare_service),
):
    try:
        return await service.candles(symbol, timeframe, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MarketUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
