# ruff: noqa: RUF001
import asyncio
import logging
import re
import time
from collections import OrderedDict

from freqtrade.data.ashare.providers import MarketHTTP, NoDataError, ProviderError
from freqtrade.data.ashare.service import CacheEntry, MarketUnavailable
from freqtrade.data.hkshare.models import (
    TIMEFRAMES,
    CandlesResponse,
    Metadata,
    QuoteResponse,
    SearchResponse,
    instrument_for,
    now_hong_kong,
    session_label,
)
from freqtrade.data.hkshare.providers import TencentHKProvider, UnsupportedInstrument


logger = logging.getLogger(__name__)


class HKShareService:
    """Bounded process-local caches, single-flight and one verified HK source."""

    def __init__(self, http: MarketHTTP | None = None):
        self.http = http or MarketHTTP()
        self.provider = TencentHKProvider(self.http)
        self.cache: OrderedDict[tuple, CacheEntry] = OrderedDict()
        self.pending: dict[tuple, asyncio.Task] = {}
        self.cooldown: dict[str, float] = {}
        self.directory = {}
        for symbol, name, kind in [
            ("hk00700", "腾讯控股", "stock"),
            ("09988.HK", "阿里巴巴-W", "stock"),
            ("02800.HK", "盈富基金", "etf"),
            ("HSI.HK", "恒生指数", "index"),
            ("HSTECH.HK", "恒生科技指数", "index"),
        ]:
            item = instrument_for(symbol, name).model_copy(update={"kind": kind})
            self.directory[item.symbol] = item

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.http.close()

    def _put(self, key, value, ttl):
        self.cache[key] = CacheEntry(value, time.monotonic() + ttl)
        self.cache.move_to_end(key)
        while len(self.cache) > 256:
            self.cache.popitem(last=False)

    def _remember(self, item):
        if len(self.directory) < 5000 or item.symbol in self.directory:
            self.directory[item.symbol] = item

    async def _single(self, key, factory):
        if key not in self.pending:
            task = asyncio.create_task(factory())
            self.pending[key] = task

            def done(finished):
                self.pending.pop(key, None)
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(done)
        return await asyncio.shield(self.pending[key])

    def capabilities(self):
        return {
            "timeframes": list(TIMEFRAMES),
            "exchanges": ["HK"],
            "kinds": ["stock", "etf", "index"],
            "currencies": ["HKD"],
            "adjustment": "raw",
            "timezone": "Asia/Hong_Kong",
            "calendar": "time_of_day_only",
            "providers": [
                {
                    "source": "tencent",
                    "enabled": True,
                    "operations": ["search", "quote", "candles"],
                    "timeframes": list(TIMEFRAMES),
                    "reason": None,
                }
            ],
        }

    async def search(self, query: str) -> SearchResponse:
        query = query.strip()
        key = ("search", query.upper())
        entry = self.cache.get(key)
        if entry and entry.expires > time.monotonic():
            return entry.value.model_copy(update={"cached": True})
        local = [
            item
            for item in self.directory.values()
            if query.upper() in item.symbol or query.casefold() in item.name.casefold()
        ][:30]
        if not query:
            return SearchResponse(items=local, source="local", cached=True)
        try:
            direct = instrument_for(query)
        except ValueError:
            if "." in query or re.fullmatch(r"[0-9]+", query):
                raise
            direct = None

        async def fetch():
            try:
                if direct:
                    response = await self.quote(direct.symbol)
                    if response.meta.stale:
                        raise MarketUnavailable("stale_identity")
                    remote = [response.instrument]
                else:
                    async with asyncio.timeout(6.5):
                        remote = await self.provider.search(query)
                merged = {item.symbol: item for item in [*local, *remote]}
                for item in remote:
                    self._remember(item)
                result = SearchResponse(items=list(merged.values())[:30], source="tencent")
                self._put(key, result, 21600)
                return result
            except ValueError:
                raise
            except Exception as exc:
                logger.info("HK-share search failed: %s", type(exc).__name__)
                return SearchResponse(
                    items=entry.value.items if entry else local,
                    source="local",
                    cached=True,
                    stale=True,
                    warning="搜索更新失败，显示已保存标的；也可输入完整代码，如 hk00700",
                )

        return await self._single(key, fetch)

    async def quote(self, symbol: str) -> QuoteResponse:
        return await self._get("quote", symbol)

    async def candles(self, symbol: str, timeframe: str, limit: int) -> CandlesResponse:
        if timeframe not in TIMEFRAMES or not 10 <= limit <= 500:
            raise ValueError("港股支持日、周、月 K；数量须为 10～500")
        return await self._get("candles", symbol, timeframe, limit)

    @staticmethod
    def _cached(entry, *, stale=False):
        result = entry.value.model_copy(deep=True)
        result.meta.cached = True
        result.meta.stale = stale
        result.meta.session = session_label(now_hong_kong())
        if stale:
            result.meta.warning = "腾讯行情更新失败，保留最近成功数据；请查看行情时间"
        return result

    async def _get(self, operation, symbol, timeframe="", limit=0):
        item = instrument_for(symbol)
        item = self.directory.get(item.symbol, item)
        key = (operation, item.symbol, timeframe, limit, "raw", "tencent", "HKD")
        entry = self.cache.get(key)
        if entry and entry.expires > time.monotonic():
            return self._cached(entry)

        async def fetch():
            reason = "cooldown"
            if self.cooldown.get(operation, 0) <= time.monotonic():
                try:
                    async with asyncio.timeout(3.5 if operation == "quote" else 6.5):
                        if operation == "quote":
                            instrument, data, data_time = await self.provider.quote(item)
                        else:
                            # Always replace the full window. Current week/month
                            # timestamps move, so merging dates duplicates periods.
                            instrument, data, data_time = await self.provider.candles(
                                item, timeframe, limit
                            )
                    clock = now_hong_kong()
                    meta = Metadata(
                        volume_unit=instrument.volume_unit,
                        volume_note=(
                            "腾讯港股指数成交量及成交额口径尚未核清，当前显示缺失"
                            if instrument.kind == "index"
                            else None
                        ),
                        data_time=data_time,
                        fetched_at=clock.isoformat(),
                        session=session_label(clock),
                        timestamp_convention="trading_date" if timeframe else "quote_time",
                    )
                    if operation == "quote":
                        result = QuoteResponse(instrument=instrument, quote=data, meta=meta)
                    else:
                        result = CandlesResponse(
                            instrument=instrument,
                            timeframe=timeframe,
                            candles=data,
                            meta=meta,
                            coverage_start=data[0].timestamp,
                            coverage_end=data[-1].timestamp,
                            requested_count=limit,
                            complete=len(data) >= limit,
                        )
                    self._remember(instrument)
                    self._put(key, result, 3 if operation == "quote" else 15)
                    return result
                except UnsupportedInstrument as exc:
                    raise ValueError(
                        "当前支持港元柜台的股票、ETF、恒指和恒生科技；暂不支持该证券类型或币种"
                    ) from exc
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
                    logger.info("HK-share tencent %s %s: %s", operation, symbol, reason)
                    if not isinstance(exc, NoDataError):
                        self.cooldown[operation] = time.monotonic() + 30
            if entry:
                return self._cached(entry, stale=True)
            if reason == "no_data":
                raise MarketUnavailable("腾讯未返回该港股行情，请核对代码、停牌情况及历史范围")
            raise MarketUnavailable("腾讯港股行情暂时不可用，请稍后刷新")

        return await self._single(key, fetch)
