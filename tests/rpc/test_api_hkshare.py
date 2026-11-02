from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from freqtrade.data.ashare.service import MarketUnavailable
from freqtrade.data.hkshare.service import HKShareService
from freqtrade.rpc.api_server.api_auth import http_basic_or_jwt_token
from freqtrade.rpc.api_server.api_hkshare import router
from freqtrade.rpc.api_server.deps import get_api_config


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(http_basic_or_jwt_token)])
    app.dependency_overrides[get_api_config] = lambda: {"username": "test", "pass" + "word": "test"}
    app.state.hkshare_service = AsyncMock(spec=HKShareService)
    with TestClient(app) as session:
        yield session, app.state.hkshare_service


@pytest.mark.parametrize(
    "endpoint", ["search", "quote?symbol=02800.HK", "candles?symbol=HSI.HK", "capabilities"]
)
def test_hk_routes_require_existing_login(client, endpoint):
    session, service = client
    assert session.get(f"/api/v1/hkshare/{endpoint}").status_code == 401
    service.quote.assert_not_called()
    service.candles.assert_not_called()
    service.search.assert_not_called()


def test_search_uses_matching_service_without_strategy_or_exchange(client):
    session, service = client
    service.search.return_value = {"items": [], "source": "tencent"}
    response = session.get("/api/v1/hkshare/search?q=700", auth=("test", "test"))
    assert response.status_code == 200 and response.json()["items"] == []
    service.search.assert_awaited_once_with("700")


@pytest.mark.parametrize("timeframe", ["1d", "1w", "1M"])
def test_native_periods_and_count_are_forwarded(client, timeframe):
    session, service = client
    service.candles.side_effect = MarketUnavailable("controlled upstream failure")
    response = session.get(
        f"/api/v1/hkshare/candles?symbol=02800.HK&timeframe={timeframe}&limit=100",
        auth=("test", "test"),
    )
    assert response.status_code == 503
    service.candles.assert_awaited_once_with("02800.HK", timeframe, 100)


@pytest.mark.parametrize(
    "query", ["timeframe=1m", "timeframe=5m", "timeframe=1h", "limit=9", "limit=501"]
)
def test_unavailable_periods_and_counts_are_422_without_network(client, query):
    session, service = client
    assert (
        session.get(
            f"/api/v1/hkshare/candles?symbol=02800.HK&{query}", auth=("test", "test")
        ).status_code
        == 422
    )
    service.candles.assert_not_called()


def test_unsupported_currency_validation_and_upstream_failure_are_distinct(client):
    session, service = client
    service.quote.side_effect = ValueError("unsupported currency")
    assert (
        session.get("/api/v1/hkshare/quote?symbol=89988.HK", auth=("test", "test")).status_code
        == 422
    )
    service.quote.side_effect = MarketUnavailable("temporary failure")
    assert (
        session.get("/api/v1/hkshare/quote?symbol=HSI.HK", auth=("test", "test")).status_code == 503
    )


def test_capabilities_expose_only_verified_tencent_periods(client):
    session, service = client
    service.capabilities.return_value = HKShareService.capabilities(None)
    response = session.get("/api/v1/hkshare/capabilities", auth=("test", "test"))
    assert response.status_code == 200
    assert response.json()["timeframes"] == ["1d", "1w", "1M"]
    assert response.json()["currencies"] == ["HKD"]
