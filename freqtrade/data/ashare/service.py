# ruff: noqa: RUF001
import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from freqtrade.data.ashare.models import (
    SHANGHAI,
    CandlesResponse,
    Metadata,
    QuoteResponse,
    SearchResponse,
    instrument_for,
    now_shanghai,
    session_label,
)
from freqtrade.data.ashare.providers import (
    EastmoneyProvider,
    MarketHTTP,
    NoDataError,
    ProviderError,
    SinaProvider,
    TencentProvider,
    search_eastmoney,
)


logger = logging.getLogger(__name__)
CACHE_VERSION = 1


class MarketUnavailable(Exception):
    pass


@dataclass
class CacheEntry:
    value: Any
    expires: float


class AShareService:
    """Process-local bounded caches, single-flight requests and bounded fallback."""

    def __init__(self, http: MarketHTTP | None = None):
        self.http = http or MarketHTTP()
        self.providers = [
            TencentProvider(self.http),
            SinaProvider(self.http),
            EastmoneyProvider(self.http),
        ]
        self.cache: OrderedDict[tuple, CacheEntry] = OrderedDict()
        self.history: OrderedDict[tuple, CacheEntry] = OrderedDict()
        self.pending: dict[tuple, asyncio.Task] = {}
        self.cooldown: dict[tuple, float] = {}
        self.directory = {
            symbol: instrument_for(symbol, name)
            for symbol, name in [
                ("600519.SH", "贵州茅台"),
                ("000001.SH", "上证指数"),
                ("510300.SH", "沪深300ETF"),
                ("000001.SZ", "平安银行"),
                ("399001.SZ", "深证成指"),
                ("159915.SZ", "创业板ETF"),
            ]
        }

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.http.close()

    @staticmethod
    def _put(cache: OrderedDict, key: tuple, value: Any, ttl: float):
        cache[key] = CacheEntry(value, time.monotonic() + ttl)
        cache.move_to_end(key)
        while len(cache) > 256:
            cache.popitem(last=False)

    def _remember(self, instrument):
        if len(self.directory) < 5000 or instrument.symbol in self.directory:
            self.directory[instrument.symbol] = instrument

    async def _single(self, key: tuple, factory):
        if key not in self.pending:
            task = asyncio.create_task(factory())
            self.pending[key] = task

            def done(finished):
                self.pending.pop(key, None)
                if not finished.cancelled():
                    finished.exception()  # Consume errors if every subscriber disconnected.

            task.add_done_callback(done)
        return await asyncio.shield(self.pending[key])

    def capabilities(self):
        return {
            "timeframes": ["1m", "1d"],
            "exchanges": ["SH", "SZ"],
            "kinds": ["stock", "index", "etf"],
            "adjustment": "raw",
            "calendar": "time_of_day_only",
            "providers": [
                {
                    "source": p.name,
                    "enabled": p.enabled,
                    "operations": sorted(p.operations),
                    "timeframes": sorted(p.timeframes),
                    "exchanges": sorted(p.exchanges),
                    "kinds": sorted(p.kinds),
                    "adjustments": sorted(p.adjustments),
                    "reason": None if p.enabled else "当前环境东财 K 线连接被关闭，备用尚未启用",
                }
                for p in self.providers
            ],
        }

    async def search(self, query: str) -> SearchResponse:
        query = query.strip()
        key = (CACHE_VERSION, "search", query.upper())
        entry = self.cache.get(key)
        if entry and entry.expires > time.monotonic():
            return entry.value.model_copy(update={"cached": True})
        if not query:
            return SearchResponse(
                items=list(self.directory.values())[:20], source="local", cached=True
            )

        async def fetch():
            local = [
                v for v in self.directory.values() if query.upper() in v.symbol or query in v.name
            ]
            if "." in query:
                item = instrument_for(query)
                try:
                    result = await self.quote(item.symbol)
                    return SearchResponse(items=[result.instrument], source=result.meta.source)
                except MarketUnavailable:
                    return SearchResponse(
                        items=local,
                        source="local",
                        stale=True,
                        warning="代码校验暂时失败，请稍后重试",
                    )
            try:
                async with asyncio.timeout(5):
                    remote = await search_eastmoney(self.http, query)
                merged = {v.symbol: v for v in [*local, *remote]}
                for item in remote:
                    self._remember(item)
                result = SearchResponse(items=list(merged.values())[:30], source="eastmoney")
                self._put(self.cache, key, result, 21600)
                return result
            except Exception as exc:
                logger.info("A-share search failed: %s", type(exc).__name__)
                items = entry.value.items if entry else local
                return SearchResponse(
                    items=items,
                    source="local",
                    cached=True,
                    stale=True,
                    warning="搜索更新失败，显示已保存标的；也可输入完整代码，例如 600519.SH",
                )

        return await self._single(key, fetch)

    async def quote(self, symbol: str) -> QuoteResponse:
        return await self._get("quote", symbol)

    async def candles(self, symbol: str, timeframe: str, limit: int) -> CandlesResponse:
        if timeframe not in {"1m", "1d"} or not 10 <= limit <= 500:
            raise ValueError("周期仅支持 1m / 1d，K 线数量须为 10～500")
        return await self._get("candles", symbol, timeframe, limit)

    async def _get(self, operation: str, symbol: str, timeframe: str = "", limit: int = 0):
        instrument = instrument_for(symbol)
        instrument = self.directory.get(instrument.symbol, instrument)
        key = (
            CACHE_VERSION,
            operation,
            instrument.symbol,
            instrument.kind,
            timeframe,
            limit,
            "raw",
        )
        now = time.monotonic()
        candidates = [
            p
            for p in self.providers
            if p.enabled
            and operation in p.operations
            and (operation != "candles" or timeframe in p.timeframes)
            and instrument.exchange in p.exchanges
            and instrument.kind in p.kinds
            and "raw" in p.adjustments
        ][:2]
        entries = [self.cache.get((*key, p.name)) for p in candidates]
        for entry in entries:
            if entry and entry.expires > now:
                result = entry.value.model_copy(deep=True)
                result.meta.cached = True
                result.meta.session = session_label(now_shanghai())
                return result

        async def fetch():
            failures = []
            for index, provider in enumerate(candidates):
                health_key = (provider.name, operation)
                if self.cooldown.get(health_key, 0) > time.monotonic():
                    failures.append("cooldown")
                    continue
                try:
                    # Includes throttling and every HTTP attempt. The two providers
                    # together cannot exceed 7s for a quote / 13s for candles.
                    async with asyncio.timeout(3.5 if operation == "quote" else 6.5):
                        if operation == "quote":
                            item, data, data_time = await provider.quote(instrument)
                        else:
                            item, data, data_time = await self._candles_via(
                                provider, instrument, timeframe, limit, key
                            )
                    clock = now_shanghai()
                    meta = Metadata(
                        source=provider.name,
                        fallback_used=index > 0,
                        volume_unit=item.volume_unit,
                        data_time=data_time,
                        fetched_at=clock.isoformat(),
                        session=session_label(clock),
                        timestamp_convention=(
                            "interval_end"
                            if timeframe == "1m"
                            else "trading_date"
                            if timeframe == "1d"
                            else "quote_time"
                        ),
                    )
                    if operation == "quote":
                        result = QuoteResponse(instrument=item, quote=data, meta=meta)
                    else:
                        result = CandlesResponse(
                            instrument=item,
                            timeframe=timeframe,
                            candles=data,
                            meta=meta,
                            coverage_start=data[0].timestamp,
                            coverage_end=data[-1].timestamp,
                            requested_count=limit,
                            complete=len(data) >= limit,
                        )
                    self._remember(item)
                    self._put(
                        self.cache, (*key, provider.name), result, 3 if operation == "quote" else 8
                    )
                    return result
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
                    failures.append(reason)
                    logger.info("A-share %s %s %s: %s", provider.name, operation, symbol, reason)
                    if not isinstance(exc, NoDataError):
                        self.cooldown[health_key] = time.monotonic() + 30
            old = [e for e in entries if e]
            if old:
                result = max(old, key=lambda e: e.expires).value.model_copy(deep=True)
                result.meta.cached = True
                result.meta.stale = True
                result.meta.warning = "行情更新失败，保留最近成功数据；请查看行情时间"
                result.meta.session = session_label(now_shanghai())
                return result
            if failures and all(reason == "no_data" for reason in failures):
                raise MarketUnavailable("上游未返回该标的行情，请核对代码、停牌情况及可用历史范围")
            raise MarketUnavailable("行情源暂时不可用，请稍后刷新")

        return await self._single(key, fetch)

    async def _candles_via(self, provider, instrument, timeframe, limit, key):
        from datetime import datetime

        today = now_shanghai().date()
        history_key = (*key, provider.name, today.isoformat())
        historical = self.history.get(history_key)
        valid = historical and historical.expires > time.monotonic()
        request_limit = min(limit, 242 if timeframe == "1m" else 2) if valid else limit
        item, bars, data_time = await provider.candles(instrument, timeframe, request_limit)
        if valid:
            merged = {bar.timestamp: bar for bar in [*historical.value, *bars]}
            bars = sorted(merged.values(), key=lambda bar: bar.timestamp)[-limit:]
        else:
            bars = bars[-limit:]
        past = [
            bar
            for bar in bars
            if datetime.fromtimestamp(bar.timestamp / 1000, SHANGHAI).date() < today
        ]
        if past and not valid:
            self._put(self.history, history_key, past, 21600)
        return item, bars, data_time
