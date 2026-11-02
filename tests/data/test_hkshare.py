import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from freqtrade.data.ashare.providers import MarketHTTP, NoDataError, ProviderError
from freqtrade.data.ashare.service import MarketUnavailable
from freqtrade.data.hkshare.models import instrument_for, session_label
from freqtrade.data.hkshare.providers import (
    TencentHKProvider,
    UnsupportedInstrument,
    identify,
    normalize_candles,
    parse_time,
)
from freqtrade.data.hkshare.service import HKShareService


def fields(code="00700", kind="GP", currency="HKD"):
    result = [""] * 78
    for index, value in {
        1: "Tencent test",
        2: code,
        3: "392.2",
        4: "383.6",
        5: "387.6",
        6: "22621767",
        30: "2026/11/02 16:08:21",
        33: "397.6",
        34: "384.6",
        37: "8881214407.65",
        63: kind,
        75: currency,
    }.items():
        result[index] = value
    return result


def mock_http(handler):
    return MarketHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def snapshot_response(*rows):
    return httpx.Response(
        200,
        content=";".join(f'v_hk{row[2]}="{"~".join(row)}"' for row in rows).encode("gb18030"),
    )


@pytest.mark.parametrize("symbol", ["700", "00700", "hk00700", "HK.00700", " 00700.hk "])
def test_numeric_symbol_aliases_are_canonical_and_safe(symbol):
    item = instrument_for(symbol)
    assert item.symbol == "00700" + ".HK"
    assert item.provider_code == "hk00700"
    assert item.currency == "HKD"


@pytest.mark.parametrize("symbol", ["HSI", "hkhsi", "HSI.HK", "HK.HSTECH", "HSTECH.HK"])
def test_supported_index_aliases(symbol):
    item = instrument_for(symbol)
    assert item.kind == "index"
    assert item.volume_unit == "unavailable"
    assert item.provider_code in {"hkHSI", "hkHSTECH"}


@pytest.mark.parametrize(
    "symbol", ["", "00000", "600519.SH", "800000", "HSCEI.HK", "700/../", "700?x=1", "00700,02800"]
)
def test_invalid_symbols_cannot_reach_upstream(symbol):
    with pytest.raises(ValueError):
        instrument_for(symbol)


@pytest.mark.parametrize(
    "stamp", ["2026/11/2 16:08:21", "2026/11/02 16:08", "2026-11-2", "2026/11/32 16:08:21"]
)
def test_timestamp_validation_does_not_accept_short_fields(stamp):
    with pytest.raises(ProviderError, match="invalid_timestamp"):
        parse_time(stamp)


def test_hong_kong_timestamps_and_trading_clock():
    now = parse_time("2026/11/02 11:45:00")
    assert now.isoformat() == "2026-11-02T11:45:00+08:00"
    assert str(now.tzinfo) == "Asia/Hong_Kong"
    assert "核对交易日" in session_label(now)
    assert "午间" in session_label(parse_time("2026/11/02 12:00:00"))
    assert "核对交易日" in session_label(parse_time("2026/11/02 15:30:00"))
    assert "非连续" in session_label(parse_time("2026/11/02 16:00:00"))
    assert "周末" in session_label(parse_time("2026/11/01 11:45:00"))


@pytest.mark.parametrize(
    "kind,currency", [("QZ", "HKD"), ("GP-FUND-REITS", "HKD"), ("GP", "CNY"), ("GP", "USD")]
)
def test_snapshot_rejects_unverified_security_types_and_currencies(kind, currency):
    with pytest.raises(UnsupportedInstrument):
        identify(instrument_for("00700"), fields(kind=kind, currency=currency))


def test_snapshot_verifies_exact_code_and_index_type():
    with pytest.raises(NoDataError):
        identify(instrument_for("00700"), fields(code="02800"))
    with pytest.raises(UnsupportedInstrument):
        identify(instrument_for("HSI"), fields(code="HSI", kind="GP"))
    with pytest.raises(UnsupportedInstrument):
        identify(instrument_for("00700"), fields(kind="ZS"))


@pytest.mark.parametrize(
    "code,kind,unit",
    [("00700", "GP", "shares"), ("02800", "GP-FUND", "fund_units"), ("HSI", "ZS", "unavailable")],
)
async def test_quote_uses_verified_hk_units_without_ashare_multipliers(code, kind, unit):
    http = mock_http(lambda request: snapshot_response(fields(code, kind)))
    try:
        item, quote, stamp = await TencentHKProvider(http).quote(instrument_for(code))
        assert item.volume_unit == unit
        assert item.kind == {"GP": "stock", "GP-FUND": "etf", "ZS": "index"}[kind]
        assert quote.price == 392.2
        assert quote.change == pytest.approx(8.6)
        assert stamp == "2026-11-02T16:08:21+08:00"
        assert quote.volume == (None if kind == "ZS" else 22621767)
        assert quote.amount == (None if kind == "ZS" else 8881214407.65)
    finally:
        await http.close()


@pytest.mark.parametrize("timeframe,field", [("1d", "day"), ("1w", "week"), ("1M", "month")])
async def test_native_unadjusted_periods_and_etf_identity(timeframe, field, monkeypatch):
    monkeypatch.setattr(
        "freqtrade.data.hkshare.providers.now_hong_kong", lambda: parse_time("2026/11/02 15:00:00")
    )

    def handler(request):
        assert request.url.path == "/appstock/app/kline/kline"
        assert request.url.params["param"] == f"hk02800,{field},,,320"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "hk02800": {
                        "qt": {"hk02800": fields("02800", "GP-FUND")},
                        field: [
                            ["2026-10-30", "10", "11", "12", "9", "1234"],
                            ["2026-11-02", "10", "11", "12", "9", "5678"],
                        ],
                    }
                },
            },
        )

    http = mock_http(handler)
    try:
        item, bars, _ = await TencentHKProvider(http).candles(
            instrument_for("02800"), timeframe, 320
        )
        assert item.kind == "etf" and item.volume_unit == "fund_units"
        assert [bar.volume for bar in bars] == [1234, 5678]
        assert bars[0].complete and not bars[-1].complete
    finally:
        await http.close()


def test_index_candle_volume_is_missing_even_when_upstream_has_a_number():
    bars = normalize_candles(
        [["2026-10-30", "10", "11", "12", "9", "204840.391"]], "1d", instrument_for("HSI")
    )
    assert bars[0].volume is None


@pytest.mark.parametrize(
    "row",
    [
        ["2026-10-30", "10", "13", "12", "9", "1"],
        ["2026-10-30", "0", "11", "12", "9", "1"],
        ["2026-10-30", "10", "11", "12", "9", "-1"],
        ["2026-10-30", "nan", "11", "12", "9", "1"],
        ["2026-10-30", "10"],
    ],
)
def test_malformed_candles_fail_instead_of_inventing_prices(row):
    with pytest.raises((ProviderError, ValueError)):
        normalize_candles([row], "1d", instrument_for("00700"))


@pytest.mark.parametrize("timeframe", ["1w", "1M"])
def test_duplicate_current_period_has_one_latest_row(timeframe):
    bars = normalize_candles(
        [["2026-11-02", "10", "11", "12", "9", "1"], ["2026-11-03", "10", "12", "13", "9", "2"]],
        timeframe,
        instrument_for("00700"),
    )
    assert len(bars) == 1
    assert bars[0].close == 12 and bars[0].volume == 2


async def test_search_verifies_etf_type_and_filters_derivatives_reits_and_cny():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "smartbox.gtimg.cn":
            assert request.url.params["t"] == "hk"
            hints = (
                "hk~02800~ETF~etf~GP^hk~80700~CNY~cny~GP^hk~00823~REIT~reit~GP^"
                "hk~13005~Warrant~qz~QZ^sh~600519~CN~cn~GP^hk~00700~Stock~stock~GP"
            )
            return httpx.Response(200, text=f"v_hint={json.dumps(hints)}")
        assert "13005" not in str(request.url) and "600519" not in str(request.url)
        return snapshot_response(
            fields("02800", "GP-FUND"),
            fields("80700", "GP", "CNY"),
            fields("00823", "GP-FUND-REITS"),
            fields("00700"),
        )

    http = mock_http(handler)
    try:
        items = await TencentHKProvider(http).search("test")
        assert [(item.provider_code, item.kind) for item in items] == [
            ("hk02800", "etf"),
            ("hk00700", "stock"),
        ]
        assert len(calls) == 2
    finally:
        await http.close()


@pytest.fixture
async def service():
    http = mock_http(lambda request: snapshot_response(fields()))
    instance = HKShareService(http)
    yield instance
    await instance.close()


async def test_concurrent_aliases_share_fetch_and_preserve_cached_metadata(service):
    provider = service.provider
    provider.quote = AsyncMock(wraps=provider.quote)
    results = await asyncio.gather(service.quote("700"), service.quote("hk00700"))
    assert results[0] == results[1]
    assert provider.quote.await_count == 1
    cached = await service.quote("HK.00700")
    assert cached.meta.cached and cached.meta.currency == "HKD"
    assert cached.meta.timezone == "Asia/Hong_Kong"
    assert cached.meta.source == cached.meta.requested_source == "tencent"
    assert not cached.meta.fallback_used


async def test_failure_retains_success_and_cooldown_does_not_overwrite_it(service):
    good = await service.quote("700")
    for entry in service.cache.values():
        entry.expires = 0
    service.provider.quote = AsyncMock(side_effect=TimeoutError())
    stale = await service.quote("700")
    assert stale.quote == good.quote
    assert stale.meta.stale and stale.meta.cached and stale.meta.warning
    assert all(not entry.value.meta.stale for entry in service.cache.values())
    assert (await service.quote("700")).meta.stale
    assert service.provider.quote.await_count == 1


async def test_unsupported_identity_is_422_style_validation_without_global_cooldown(service):
    service.provider.quote = AsyncMock(side_effect=UnsupportedInstrument("currency"))
    with pytest.raises(ValueError, match="币种"):
        await service.quote("89988")
    assert not service.cooldown and not service.cache


async def test_direct_search_checks_identity_and_bad_query_skips_network(service):
    result = await service.search("700")
    assert result.items[0].provider_code == "hk00700"
    assert (await service.search("700")).cached
    with pytest.raises(ValueError):
        await service.search("600519.SH")
    with pytest.raises(ValueError):
        await service.search("800000")


async def test_search_failure_keeps_local_index_and_explains_staleness(service):
    service.provider.search = AsyncMock(side_effect=TimeoutError())
    result = await service.search("恒生")
    assert {item.provider_code for item in result.items} == {"hkHSI", "hkHSTECH"}
    assert result.stale and result.warning


@pytest.mark.parametrize("timeframe", ["1w", "1M"])
async def test_updated_long_period_replaces_window_instead_of_appending_dates(service, timeframe):
    item = instrument_for("700")
    first = normalize_candles(
        [["2026-10-30", "10", "11", "12", "9", "1"], ["2026-11-02", "10", "11", "12", "9", "2"]],
        timeframe,
        item,
    )
    second = normalize_candles(
        [["2026-10-30", "10", "11", "12", "9", "1"], ["2026-11-03", "10", "12", "13", "9", "3"]],
        timeframe,
        item,
    )
    service.provider.candles = AsyncMock(side_effect=[(item, first, None), (item, second, None)])
    await service.candles("700", timeframe, 320)
    for entry in service.cache.values():
        entry.expires = 0
    updated = await service.candles("700", timeframe, 320)
    assert len(updated.candles) == 2
    assert updated.candles[-1].close == 12 and updated.candles[-1].volume == 3
    assert service.provider.candles.await_args.args[-1] == 320


async def test_bad_period_count_and_symbols_skip_provider(service):
    service.provider.candles = AsyncMock()
    for timeframe, count in [("1m", 320), ("1d", 9), ("1M", 501)]:
        with pytest.raises(ValueError):
            await service.candles("700", timeframe, count)
    with pytest.raises(ValueError):
        await service.quote("600519.SH")
    service.provider.candles.assert_not_called()


async def test_cancelled_subscriber_does_not_cancel_other_subscribers(service):
    provider = service.provider
    provider.quote = AsyncMock(wraps=provider.quote)
    task = asyncio.create_task(service.quote("700"))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await service.quote("700")).quote.price == 392.2
    assert provider.quote.await_count == 1


async def test_cache_capacity_and_capabilities(service):
    for index in range(300):
        service._put(("test", index), index, 1)
    assert len(service.cache) == 256
    assert ("test", 0) not in service.cache
    assert service.capabilities()["timeframes"] == ["1d", "1w", "1M"]
    assert [p["source"] for p in service.capabilities()["providers"]] == ["tencent"]


async def test_total_timeout_is_bounded_including_http_throttling(service, monkeypatch):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(
        "freqtrade.data.hkshare.service.asyncio.timeout", lambda seconds: real_timeout(0.02)
    )

    async def slow(instrument):
        await asyncio.sleep(1)

    service.provider.quote = slow
    start = time.monotonic()
    with pytest.raises(MarketUnavailable):
        await service.quote("700")
    assert time.monotonic() - start < 0.4
