from fastapi import APIRouter, Depends, HTTPException, Query, Request

from freqtrade.data.ashare.models import CandlesResponse, QuoteResponse, SearchResponse
from freqtrade.data.ashare.service import AShareService, MarketUnavailable


router = APIRouter(prefix="/ashare", tags=["A-share market data"])


def get_ashare_service(request: Request) -> AShareService:
    return request.app.state.ashare_service


@router.get("/capabilities")
async def capabilities(service: AShareService = Depends(get_ashare_service)):
    return service.capabilities()


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(default="", max_length=80, pattern=r"^[^\x00-\x1f]*$"),
    service: AShareService = Depends(get_ashare_service),
):
    try:
        return await service.search(q)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/quote", response_model=QuoteResponse)
async def quote(
    symbol: str = Query(max_length=12), service: AShareService = Depends(get_ashare_service)
):
    try:
        return await service.quote(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MarketUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/candles", response_model=CandlesResponse)
async def candles(
    symbol: str = Query(max_length=12),
    timeframe: str = Query(default="1m", pattern=r"^(1m|1d)$"),
    limit: int = Query(default=320, ge=10, le=500),
    service: AShareService = Depends(get_ashare_service),
):
    try:
        return await service.candles(symbol, timeframe, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MarketUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
