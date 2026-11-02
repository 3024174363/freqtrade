"""Small HTTP adapters. No broker connection or exchange/strategy dependency."""

import asyncio
import json
import logging
import math
import re
import secrets
from datetime import datetime
from typing import Any

import httpx

from freqtrade.data.ashare.models import (
    SHANGHAI,
    Candle,
    Instrument,
    Quote,
    instrument_for,
    now_shanghai,
)


logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """A sanitized upstream failure, safe to record in diagnostics."""


class NoDataError(ProviderError):
    pass


def number(value: Any, *, optional: bool = False) -> float | None:
    if optional and value in (None, "", "-", "--"):
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ProviderError("invalid_number")
    return result


def quote_values(
    price: Any, previous: Any, opening: Any, high: Any, low: Any, volume: Any, amount: Any
) -> Quote:
    values = [number(v, optional=True) for v in (price, previous, opening, high, low)]
    if any(value is not None and value < 0 for value in values):
        raise ProviderError("negative_price")
    # A suspended/unopened security can have a zero latest price; do not invent a trade.
    p, prev, op, hi, lo = [v if v is not None and v > 0 else None for v in values]
    vol, amt = number(volume, optional=True), number(amount, optional=True)
    if (vol is not None and vol < 0) or (amt is not None and amt < 0):
        raise ProviderError("negative_volume")
    if hi is not None and lo is not None and hi < lo:
        raise ProviderError("invalid_ohlc")
    if (
        hi is not None
        and lo is not None
        and any(value is not None and not lo <= value <= hi for value in (p, op))
    ):
        raise ProviderError("invalid_ohlc")
    change = p - prev if p is not None and prev is not None else None
    return Quote(
        price=p,
        previous_close=prev,
        open=op,
        high=hi,
        low=lo,
        change=change,
        change_percent=change / prev * 100 if change is not None else None,
        volume=vol,
        amount=amt,
    )


def parse_time(value: str) -> datetime:
    # strptime accepts short fields: choose the exact length so 13:41 never
    # becomes 13:04:01 when a minute stamp is parsed with a seconds format.
    formats = {
        14: "%Y%m%d%H%M%S",
        12: "%Y%m%d%H%M",
        19: "%Y-%m-%d %H:%M:%S",
        16: "%Y-%m-%d %H:%M",
        10: "%Y-%m-%d",
    }
    try:
        fmt = formats[len(value)]
        parsed = datetime.strptime(value, fmt).replace(tzinfo=SHANGHAI)
        if parsed.strftime(fmt) != value:
            raise ValueError("timestamp format mismatch")
        return parsed
    except (KeyError, ValueError) as exc:
        raise ProviderError("invalid_timestamp") from exc


def normalize_candles(rows: list, timeframe: str, factor: int) -> list[Candle]:
    if not rows:
        raise NoDataError("no_data")
    now = now_shanghai()
    result: dict[int, Candle] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise ProviderError("invalid_candle")
        stamp = parse_time(str(row[0]))
        op, close, high, low = [number(v) for v in row[1:5]]
        volume = number(row[5], optional=True)
        if min(op, close, high, low) <= 0 or high < max(op, close, low) or low > min(op, close):
            raise ProviderError("invalid_ohlc")
        if volume is not None and volume < 0:
            raise ProviderError("negative_volume")
        ts = int(stamp.timestamp() * 1000)
        result[ts] = Candle(
            timestamp=ts,
            open=op,
            close=close,
            high=high,
            low=low,
            volume=volume * factor if volume is not None else None,
            complete=stamp <= now if timeframe == "1m" else stamp.date() < now.date(),
        )
    return sorted(result.values(), key=lambda bar: bar.timestamp)


class MarketHTTP:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(
            timeout=3,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=6),
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def get(self, source: str, url: str, params: dict | None = None) -> httpx.Response:
        lock = self._locks.setdefault(source, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            spacing = 0.35 if source == "eastmoney" else 0.12
            delay = self._last.get(source, 0) + spacing - loop.time()
            if delay > 0:
                await asyncio.sleep(delay + secrets.randbelow(81) / 1000)
            for attempt in range(2):
                self._last[source] = loop.time()
                try:
                    response = await self.client.get(url, params=params)
                    break
                except httpx.TransportError:
                    if attempt:
                        raise
                    # The caller's timeout includes throttling and this one retry.
                    await asyncio.sleep(spacing + secrets.randbelow(81) / 1000)
            response.raise_for_status()
            return response

    async def close(self):
        await self.client.aclose()


class ProviderCapabilities:
    exchanges = {"SH", "SZ"}
    kinds = {"stock", "index", "etf"}
    adjustments = {"raw"}


class TencentProvider(ProviderCapabilities):
    name = "tencent"
    enabled = True
    operations = {"quote", "candles"}
    timeframes = {"1m", "1d"}

    def __init__(self, http: MarketHTTP):
        self.http = http

    async def quote(self, instrument: Instrument) -> tuple[Instrument, Quote, str]:
        response = await self.http.get(
            self.name, "https://qt.gtimg.cn/q=" + instrument.provider_code
        )
        match = re.search(
            rf'v_{instrument.provider_code}="([^"]*)"', response.content.decode("gb18030")
        )
        fields = match.group(1).split("~") if match else []
        if len(fields) < 46 or fields[2] != instrument.symbol[:6]:
            raise NoDataError("no_data")
        volume = number(fields[6], optional=True)
        if volume is not None and instrument.kind != "index":
            volume *= 100  # Tencent stock/ETF lots; index aggregate stays in reported lots.
        amount = number(fields[37], optional=True)
        quote = quote_values(
            fields[3],
            fields[4],
            fields[5],
            fields[33],
            fields[34],
            volume,
            amount * 10000 if amount is not None else None,
        )
        return (
            instrument.model_copy(update={"name": fields[1]}),
            quote,
            parse_time(fields[30]).isoformat(),
        )

    async def candles(self, instrument: Instrument, timeframe: str, limit: int):
        code = instrument.provider_code
        if timeframe == "1m":
            url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
            param, field = f"{code},m1,,{limit}", "m1"
        else:
            # Non-fq endpoint: the fq endpoint may return qfqday even without a qfq flag.
            url = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
            param, field = f"{code},day,,,{limit}", "day"
        response = await self.http.get(self.name, url, {"param": param})
        data = response.json().get("data", {}).get(code, {})
        qt = data.get("qt", {}).get(code, [])
        if len(qt) < 31 or qt[2] != instrument.symbol[:6]:
            raise NoDataError("no_data")
        rows = data.get(field, [])
        bars = normalize_candles(rows, timeframe, 1 if instrument.kind == "index" else 100)
        return (
            instrument.model_copy(update={"name": qt[1]}),
            bars,
            parse_time(qt[30]).isoformat(),
        )


class SinaProvider(ProviderCapabilities):
    name = "sina"
    enabled = True
    operations = {"quote"}
    timeframes: set[str] = set()

    def __init__(self, http: MarketHTTP):
        self.http = http

    async def quote(self, instrument: Instrument):
        response = await self.http.get(
            self.name, "https://hq.sinajs.cn/list=" + instrument.provider_code
        )
        match = re.search(
            rf'hq_str_{instrument.provider_code}="([^"]*)"', response.content.decode("gb18030")
        )
        fields = match.group(1).split(",") if match else []
        if len(fields) < 32:
            raise NoDataError("no_data")
        quote = quote_values(
            fields[3], fields[2], fields[1], fields[4], fields[5], fields[8], fields[9]
        )
        return (
            instrument.model_copy(update={"name": fields[0]}),
            quote,
            parse_time(f"{fields[30]} {fields[31]}").isoformat(),
        )


class EastmoneyProvider(ProviderCapabilities):
    name = "eastmoney"
    # The K-line host closes connections in this environment. Keep the parser and
    # routing tests, but do not advertise an unverified adapter as a working backup.
    enabled = False
    operations = {"candles"}
    timeframes = {"1m", "1d"}

    def __init__(self, http: MarketHTTP):
        self.http = http

    async def candles(self, instrument: Instrument, timeframe: str, limit: int):
        response = await self.http.get(
            self.name,
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": f"{1 if instrument.exchange == 'SH' else 0}.{instrument.symbol[:6]}",
                "klt": "1" if timeframe == "1m" else "101",
                "fqt": "0",
                "lmt": str(limit),
                "beg": "0",
                "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            },
        )
        data = response.json().get("data") or {}
        if data.get("code") != instrument.symbol[:6]:
            raise NoDataError("no_data")
        rows = []
        for line in data.get("klines") or []:
            fields = line.split(",")
            rows.append([fields[0], fields[1], fields[2], fields[3], fields[4], fields[5]])
        bars = normalize_candles(rows, timeframe, 1 if instrument.kind == "index" else 100)
        return (
            instrument.model_copy(update={"name": data.get("name") or instrument.name}),
            bars,
            None,
        )


async def search_eastmoney(http: MarketHTTP, query: str) -> list[Instrument]:
    response = await http.get(
        "eastmoney",
        "https://searchapi.eastmoney.com/api/suggest/get",
        {"input": query, "type": "14", "count": "30"},
    )
    payload = json.loads(response.content)
    table = payload.get("QuotationCodeTable") or {}
    if table.get("Status") not in (None, 0):
        raise ProviderError("search_failed")
    result = []
    for row in table.get("Data") or []:
        market, _, code = str(row.get("QuoteID", "")).partition(".")
        classify = row.get("Classify")
        if market not in {"0", "1"} or classify not in {"AStock", "Index", "Fund"}:
            continue
        if classify == "Fund" and "ETF" not in str(row.get("Name", "")).upper():
            continue
        try:
            instrument = instrument_for(
                f"{code}.{'SH' if market == '1' else 'SZ'}", row.get("Name") or code
            )
        except ValueError:
            continue
        if {"AStock": "stock", "Index": "index", "Fund": "etf"}[classify] != instrument.kind:
            continue
        result.append(instrument)
    return result
