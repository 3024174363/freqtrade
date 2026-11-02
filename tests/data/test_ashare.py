import asyncio
import time
from datetime import datetime

import httpx
import pytest

from freqtrade.data.ashare.models import SHANGHAI, instrument_for, session_label
from freqtrade.data.ashare.providers import (
    EastmoneyProvider,
    MarketHTTP,
    NoDataError,
    ProviderCapabilities,
    ProviderError,
    SinaProvider,
    TencentProvider,
    normalize_candles,
    parse_time,
    quote_values,
    search_eastmoney,
)
from freqtrade.data.ashare.service import AShareService, MarketUnavailable


def tencent_fields(code="600519", name="Test stock"):
    fields = [""] * 61
    for index, value in {
        1: name,
        2: code,
        3: "12",
        4: "10",
        5: "11",
        6: "125",
        30: "20261102134131",
        33: "13",
        34: "10",
        37: "15",
    }.items():
        fields[index] = value
    return fields


def mock_http(handler):
    return MarketHTTP(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(
    "symbol,kind,provider,unit",
    [
        ("600519.SH", "stock", "sh600519", "shares"),
        ("000001.SH", "index", "sh000001", "index_lots"),
        ("000001.SZ", "stock", "sz000001", "shares"),
        ("399001.SZ", "index", "sz399001", "index_lots"),
        ("510300.SH", "etf", "sh510300", "fund_units"),
        ("159915.SZ", "etf", "sz159915", "fund_units"),
    ],
)
def test_symbol_identity(symbol, kind, provider, unit):
    item = instrument_for(symbol.lower())
    assert (item.symbol, item.kind, item.provider_code, item.volume_unit) == (
        symbol,
        kind,
        provider,
        unit,
    )


@pytest.mark.parametrize("symbol", ["000001", "920001.BJ", "200001.SZ", "113001.SH", "abc.SH"])
def test_invalid_symbols_never_reach_provider(symbol):
    with pytest.raises(ValueError):
        instrument_for(symbol)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("202611021341", "2026-11-02T13:41:00+08:00"),
        ("20261102134131", "2026-11-02T13:41:31+08:00"),
        ("2026-11-02", "2026-11-02T00:00:00+08:00"),
    ],
)
def test_timestamp_is_exact_and_aware(value, expected):
    assert parse_time(value).isoformat() == expected


def test_candles_sorted_deduplicated_and_unfinished(monkeypatch):
    monkeypatch.setattr(
        "freqtrade.data.ashare.providers.now_shanghai", lambda: parse_time("20261102134131")
    )
    rows = [
        ["202611021342", "11", "12", "13", "10", "7"],
        ["202611021340", "10", "11", "12", "9", "3"],
        ["202611021342", "11", "12", "13", "10", "8"],
    ]
    bars = normalize_candles(rows, "1m", 100)
    assert len(bars) == 2
    assert bars[0].complete and not bars[1].complete
    assert bars[1].volume == 800
    assert datetime.fromtimestamp(bars[1].timestamp / 1000, SHANGHAI).minute == 42


@pytest.mark.parametrize(
    "row",
    [
        ["202611021341", "10", "11", "9", "8", "1"],
        ["202611021341", "10", "11", "12", "9", "-1"],
        ["202611021341", "nan", "11", "12", "9", "1"],
    ],
)
def test_invalid_candles_rejected(row):
    with pytest.raises(ProviderError):
        normalize_candles([row], "1m", 100)


@pytest.mark.parametrize(
    "symbol,factor", [("600519.SH", 100), ("510300.SH", 100), ("000001.SH", 1)]
)
async def test_tencent_stock_etf_index_units(symbol, factor):
    item = instrument_for(symbol)
    fields = tencent_fields(item.symbol[:6])

    def handler(request):
        return httpx.Response(
            200, content=f'v_{item.provider_code}="{"~".join(fields)}";'.encode("gb18030")
        )

    http = mock_http(handler)
    try:
        _, quote, stamp = await TencentProvider(http).quote(item)
        assert quote.volume == 125 * factor
        assert quote.change_percent == 20
        assert quote.amount == 150000
        assert stamp.endswith("+08:00")
    finally:
        await http.close()


async def test_sina_quote_parser_preserves_shares():
    fields = [""] * 33
    for index, value in {
        0: "Test stock",
        1: "11",
        2: "10",
        3: "12",
        4: "13",
        5: "10",
        8: "12509",
        9: "150000",
        30: "2026-11-02",
        31: "13:41:31",
    }.items():
        fields[index] = value
    http = mock_http(
        lambda request: httpx.Response(
            200, content=f'var hq_str_sh600519="{",".join(fields)}";'.encode("gb18030")
        )
    )
    try:
        _, quote, _ = await SinaProvider(http).quote(instrument_for("600519.SH"))
        assert quote.volume == 12509
    finally:
        await http.close()


@pytest.mark.parametrize("symbol,factor", [("000001.SH", 1), ("399001.SZ", 0.01)])
async def test_sina_index_snapshot_volume_is_market_specific(symbol, factor):
    item = instrument_for(symbol)
    fields = [""] * 33
    for index, value in {
        0: "Index",
        1: "11",
        2: "10",
        3: "12",
        4: "13",
        5: "10",
        8: "12509",
        9: "150000",
        30: "2026-11-02",
        31: "13:41:31",
    }.items():
        fields[index] = value
    http = mock_http(
        lambda request: httpx.Response(
            200, content=f'var hq_str_{item.provider_code}="{",".join(fields)}";'.encode("gb18030")
        )
    )
    try:
        _, quote, _ = await SinaProvider(http).quote(item)
        assert quote.volume == 12509 * factor
    finally:
        await http.close()


@pytest.mark.parametrize(
    "timeframe,field",
    [
        ("1m", "m1"),
        ("5m", "m5"),
        ("15m", "m15"),
        ("30m", "m30"),
        ("1h", "m60"),
        ("1d", "day"),
        ("1w", "week"),
        ("1M", "month"),
    ],
)
async def test_tencent_raw_endpoint_and_minutes(timeframe, field):
    def handler(request):
        assert "fqkline" not in request.url.path
        assert f",{field}," in request.url.params["param"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "sh600519": {
                        "qt": {"sh600519": tencent_fields()},
                        field: [
                            [
                                "202611021341"
                                if field.startswith("m") and field != "month"
                                else "2026-11-02",
                                "10",
                                "11",
                                "12",
                                "9",
                                "3",
                            ]
                        ],
                    }
                }
            },
        )

    http = mock_http(handler)
    try:
        _, bars, _ = await TencentProvider(http).candles(
            instrument_for("600519.SH"), timeframe, 320
        )
        assert bars[0].volume == 300
    finally:
        await http.close()


@pytest.mark.parametrize(
    "timeframe,scale", [("5m", "5"), ("15m", "15"), ("30m", "30"), ("1h", "60"), ("1d", "240")]
)
@pytest.mark.parametrize("symbol,factor", [("600519.SH", 1), ("510300.SH", 1), ("000001.SH", 0.01)])
async def test_sina_candle_periods_and_volume_units(timeframe, scale, symbol, factor):
    def handler(request):
        assert request.url.params["scale"] == scale
        assert request.url.params["symbol"] == instrument_for(symbol).provider_code
        return httpx.Response(
            200,
            json=[
                {
                    "day": "2026-11-02" if timeframe == "1d" else "2026-11-02 15:00:00",
                    "open": "10",
                    "close": "11",
                    "high": "12",
                    "low": "9",
                    "volume": "12509",
                }
            ],
        )

    http = mock_http(handler)
    try:
        item, bars, stamp = await SinaProvider(http).candles(instrument_for(symbol), timeframe, 320)
        assert bars[0].volume == 12509 * factor
        assert item.symbol == symbol and stamp is None
    finally:
        await http.close()


@pytest.mark.parametrize("timeframe,date", [("1w", "2026-11-02"), ("1M", "2026-11-02")])
def test_current_week_and_month_remain_unfinished_until_period_changes(
    monkeypatch, timeframe, date
):
    monkeypatch.setattr(
        "freqtrade.data.ashare.providers.now_shanghai", lambda: parse_time("20261104134131")
    )
    rows = [[date, "10", "11", "12", "9", "3"], ["2026-10-30", "10", "11", "12", "9", "3"]]
    bars = normalize_candles(rows, timeframe, 100)
    assert bars[0].complete and not bars[1].complete


async def test_eastmoney_raw_parser_is_disabled_until_verified():
    def handler(request):
        assert request.url.params["fqt"] == "0"
        return httpx.Response(
            200,
            json={
                "data": {
                    "code": "600519",
                    "name": "Test",
                    "klines": ["2026-11-02,10,11,12,9,3,3000"],
                }
            },
        )

    http = mock_http(handler)
    try:
        provider = EastmoneyProvider(http)
        assert provider.enabled is False
        _, bars, _ = await provider.candles(instrument_for("600519.SH"), "1d", 10)
        assert bars[0].volume == 300
    finally:
        await http.close()


async def test_search_filters_markets_and_keeps_ambiguous_codes_distinct():
    rows = [
        {"QuoteID": "1.000001", "Classify": "Index", "Name": "Shanghai index"},
        {"QuoteID": "0.000001", "Classify": "AStock", "Name": "Ping An"},
        {"QuoteID": "150.000001", "Classify": "OTCFUND", "Name": "Fund"},
        {"QuoteID": "1.510300", "Classify": "Fund", "Name": "CSI 300 ETF"},
        {"QuoteID": "0.920001", "Classify": "AStock", "Name": "Beijing"},
    ]
    http = mock_http(
        lambda request: httpx.Response(200, json={"QuotationCodeTable": {"Data": rows}})
    )
    try:
        result = await search_eastmoney(http, "000001")
        assert [item.symbol for item in result] == ["000001.SH", "000001.SZ", "510300.SH"]
    finally:
        await http.close()


class FakeProvider(ProviderCapabilities):
    enabled = True
    operations = {"quote", "candles"}
    timeframes = {"1m", "1d"}

    def __init__(self, name="tencent", fail=None):
        self.name = name
        self.fail = fail
        self.calls = []
        self.price = 11

    async def quote(self, instrument):
        self.calls.append(instrument.symbol)
        await asyncio.sleep(0)
        if self.fail:
            raise self.fail
        return (
            instrument,
            quote_values(self.price, 10, 10, 15, 9, 300, 3300),
            "2026-11-02T13:41:00+08:00",
        )

    async def candles(self, instrument, timeframe, limit):
        self.calls.append((instrument.symbol, timeframe, limit))
        if self.fail:
            raise self.fail
        rows = [
            ["2026-10-30", "10", "11", "12", "9", "3"],
            ["2026-11-02", "10", str(self.price), "15", "9", "3"],
        ]
        return instrument, normalize_candles(rows, timeframe, 100), None


@pytest.fixture
async def service():
    instance = AShareService(mock_http(lambda request: httpx.Response(500)))
    yield instance
    await instance.close()


async def test_success_does_not_call_backup_and_singleflight(service):
    primary, backup = FakeProvider(), FakeProvider("sina")
    service.providers = [primary, backup]
    results = await asyncio.gather(*(service.quote("600519.SH") for _ in range(8)))
    assert primary.calls == ["600519.SH"]
    assert not backup.calls
    assert all(result.meta.source == "tencent" for result in results)
    cached = await service.quote("600519.SH")
    assert cached.meta.cached and not cached.meta.stale


@pytest.mark.parametrize(
    "failure", [NoDataError("no_data"), TimeoutError(), ProviderError("invalid_ohlc")]
)
async def test_failed_primary_falls_back_with_real_provenance(service, failure):
    primary, backup = FakeProvider(fail=failure), FakeProvider("sina")
    service.providers = [primary, backup]
    response = await service.quote("510300.SH")
    assert response.meta.source == "sina" and response.meta.fallback_used
    assert response.meta.volume_unit == "fund_units" and response.meta.adjustment == "raw"
    assert backup.calls == ["510300.SH"]


async def test_manual_source_never_uses_other_provider_or_their_cache(service):
    primary, backup = FakeProvider(), FakeProvider("sina")
    service.providers = [primary, backup]
    await service.quote("600519.SH")  # Fresh Tencent cache must not satisfy Sina.
    response = await service.quote("600519.SH", source="sina")
    assert response.meta.source == "sina" and response.meta.requested_source == "sina"
    assert not response.meta.fallback_used
    assert primary.calls == ["600519.SH"] and backup.calls == ["600519.SH"]
    for key, entry in service.cache.items():
        if key[-1] == "sina":
            entry.expires = 0
    backup.fail = TimeoutError()
    response = await service.quote("600519.SH", source="sina")
    assert response.meta.source == "sina" and response.meta.stale
    assert len(primary.calls) == 1


async def test_manual_source_failure_without_own_cache_does_not_fall_back(service):
    primary, backup = FakeProvider(), FakeProvider("sina", fail=NoDataError("no_data"))
    service.providers = [primary, backup]
    await service.quote("600519.SH")
    with pytest.raises(MarketUnavailable):
        await service.quote("600519.SH", source="sina")
    assert primary.calls == ["600519.SH"] and backup.calls == ["600519.SH"]


async def test_manual_and_auto_inflight_results_do_not_cross(service):
    primary, backup = FakeProvider(), FakeProvider("sina")
    service.providers = [primary, backup]
    automatic, manual = await asyncio.gather(
        service.quote("600519.SH"), service.quote("600519.SH", source="sina")
    )
    assert automatic.meta.source == "tencent" and manual.meta.source == "sina"


async def test_cached_provenance_tracks_request_mode(service):
    primary, backup = FakeProvider(fail=TimeoutError()), FakeProvider("sina")
    service.providers = [primary, backup]
    automatic = await service.quote("600519.SH")
    assert automatic.meta.fallback_used
    manual = await service.quote("600519.SH", source="sina")
    assert manual.meta.cached and not manual.meta.fallback_used
    assert manual.meta.requested_source == "sina"
    automatic = await service.quote("600519.SH")
    assert automatic.meta.cached and automatic.meta.fallback_used


async def test_invalid_manual_capabilities_skip_network(service):
    primary, backup, disabled = FakeProvider(), FakeProvider("sina"), FakeProvider("eastmoney")
    backup.timeframes = {"5m", "1d"}
    disabled.enabled = False
    service.providers = [primary, backup, disabled]
    for source, timeframe in [("sina", "1m"), ("eastmoney", "1d"), ("unknown", "1d")]:
        with pytest.raises(ValueError):
            await service.candles("600519.SH", timeframe, 320, source=source)
    assert not primary.calls and not backup.calls and not disabled.calls


@pytest.mark.parametrize("timeframe", ["1w", "1M"])
async def test_unfinished_long_period_replaced_without_duplicate(service, monkeypatch, timeframe):
    monkeypatch.setattr(
        "freqtrade.data.ashare.service.now_shanghai", lambda: parse_time("20261104134131")
    )

    class UpdatingProvider(FakeProvider):
        timeframes = {"1w", "1M"}

        async def candles(self, instrument, timeframe, limit):
            date = "2026-11-02" if not self.calls else "2026-11-03"
            self.calls.append((timeframe, limit))
            bars = normalize_candles(
                [["2026-10-30", "10", "11", "12", "9", "3"], [date, "10", "11", "12", "9", "3"]],
                timeframe,
                100,
            )
            return instrument, bars, None

    provider = UpdatingProvider()
    service.providers = [provider]
    await service.candles("600519.SH", timeframe, 320)
    for entry in service.cache.values():
        entry.expires = 0
    updated = await service.candles("600519.SH", timeframe, 320)
    assert len(updated.candles) == 2
    assert provider.calls == [(timeframe, 320), (timeframe, 320)]
    assert not service.history


async def test_exhausted_sources_return_stale_without_overwriting_success(service):
    primary = FakeProvider()
    service.providers = [primary]
    good = await service.quote("600519.SH")
    for entry in service.cache.values():
        entry.expires = 0
    primary.fail = TimeoutError()
    stale = await service.quote("600519.SH")
    assert stale.meta.stale and stale.meta.cached and stale.meta.warning
    assert stale.quote == good.quote
    assert all(not entry.value.meta.stale for entry in service.cache.values())
    again = await service.quote("600519.SH")
    assert again.meta.stale and len(primary.calls) == 2


async def test_no_data_and_invalid_parameters_do_not_become_server_error(service):
    primary = FakeProvider(fail=NoDataError("no_data"))
    service.providers = [primary]
    with pytest.raises(MarketUnavailable):
        await service.quote("600519.SH")
    with pytest.raises(ValueError):
        await service.quote("000001")
    with pytest.raises(ValueError):
        await service.candles("600519.SH", "2h", 320)
    assert len(primary.calls) == 1


async def test_capability_skip_and_cache_identity(service):
    unsupported = FakeProvider("daily_only")
    unsupported.timeframes = {"1d"}
    disabled = FakeProvider("disabled")
    disabled.enabled = False
    primary = FakeProvider()
    service.providers = [unsupported, disabled, primary]
    await service.candles("600519.SH", "1m", 320)
    assert not unsupported.calls and not disabled.calls
    await service.quote("000001.SH")
    await service.quote("000001.SZ")
    assert "000001.SH" in unsupported.calls and "000001.SZ" in unsupported.calls
    assert len(service.cache) == 3


async def test_today_daily_candle_is_refetched_and_replaced(service, monkeypatch):
    monkeypatch.setattr(
        "freqtrade.data.ashare.service.now_shanghai", lambda: parse_time("20261102134131")
    )
    primary = FakeProvider()
    service.providers = [primary]
    first = await service.candles("600519.SH", "1d", 320)
    assert len(service.history) == 1
    assert all(len(entry.value) == 1 for entry in service.history.values())
    for entry in service.cache.values():
        entry.expires = 0
    primary.price = 12
    updated = await service.candles("600519.SH", "1d", 320)
    assert first.candles[-1].close == 11 and updated.candles[-1].close == 12
    assert primary.calls[-1] == ("600519.SH", "1d", 2)
    assert len(updated.candles) == 2 and not updated.complete


async def test_cancelled_subscriber_does_not_cancel_shared_fetch(service):
    primary = FakeProvider()
    service.providers = [primary]
    task = asyncio.create_task(service.quote("600519.SH"))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    result = await service.quote("600519.SH")
    assert result.quote.price == 11 and primary.calls == ["600519.SH"]


async def test_provider_timeout_includes_throttle_and_bounds_fallback(service, monkeypatch):
    real_timeout = asyncio.timeout
    monkeypatch.setattr(
        "freqtrade.data.ashare.service.asyncio.timeout", lambda seconds: real_timeout(0.02)
    )

    class SlowProvider(FakeProvider):
        async def quote(self, instrument):
            await asyncio.sleep(1)

    service.providers = [SlowProvider(), SlowProvider("sina")]
    start = time.monotonic()
    with pytest.raises(MarketUnavailable):
        await service.quote("600519.SH")
    assert time.monotonic() - start < 0.4


def test_session_labels_do_not_assert_holiday_calendar():
    assert "以行情时间为准" in session_label(parse_time("20261102134131"))
    assert "午间" in session_label(parse_time("20261102120000"))
    assert "周末" in session_label(parse_time("20261101120000"))


async def test_http_retries_transport_error_once_but_not_rate_limit():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ConnectError("connection failed")
        return httpx.Response(200 if request.url.path == "/retry" else 429)

    http = mock_http(handler)
    try:
        assert (await http.get("tencent", "https://example.test/retry")).status_code == 200
        with pytest.raises(httpx.HTTPStatusError):
            await http.get("tencent", "https://example.test/rate_limit")
        assert calls == ["/retry", "/retry", "/rate_limit"]
    finally:
        await http.close()
