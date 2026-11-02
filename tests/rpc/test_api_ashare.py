from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from freqtrade.data.ashare.service import AShareService, MarketUnavailable
from freqtrade.rpc.api_server.api_ashare import router
from freqtrade.rpc.api_server.api_auth import http_basic_or_jwt_token
from freqtrade.rpc.api_server.deps import get_api_config


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1", dependencies=[Depends(http_basic_or_jwt_token)])
    app.dependency_overrides[get_api_config] = lambda: {"username": "test", "password": "test"}
    app.state.ashare_service = AsyncMock(spec=AShareService)
    with TestClient(app) as session:
        yield session, app.state.ashare_service


@pytest.mark.parametrize(
    "endpoint", ["search", "quote?symbol=600519.SH", "candles?symbol=600519.SH", "capabilities"]
)
def test_market_routes_require_existing_auth(client, endpoint):
    session, service = client
    response = session.get(f"/api/v1/ashare/{endpoint}")
    assert response.status_code == 401
    service.quote.assert_not_called()


def test_search_authenticated_without_strategy_or_exchange(client):
    session, service = client
    service.search.return_value = {"items": [], "source": "local"}
    response = session.get("/api/v1/ashare/search", auth=("test", "test"))
    assert response.status_code == 200
    assert response.json()["items"] == []
    service.search.assert_awaited_once_with("")


def test_market_failure_is_503_and_bad_parameters_are_422(client):
    session, service = client
    service.quote.side_effect = MarketUnavailable("temporarily unavailable")
    response = session.get("/api/v1/ashare/quote?symbol=600519.SH", auth=("test", "test"))
    assert response.status_code == 503
    service.quote.side_effect = ValueError("invalid code")
    assert (
        session.get("/api/v1/ashare/quote?symbol=000001", auth=("test", "test")).status_code == 422
    )
    assert (
        session.get(
            "/api/v1/ashare/candles?symbol=600519.SH&timeframe=5m", auth=("test", "test")
        ).status_code
        == 422
    )
    service.candles.assert_not_called()
