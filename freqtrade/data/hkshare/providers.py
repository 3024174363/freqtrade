"""Tencent HKD snapshots and unadjusted daily/weekly/monthly candles."""

import json
import re
from datetime import datetime

from freqtrade.data.ashare.models import Candle
from freqtrade.data.ashare.providers import (
    MarketHTTP,
    NoDataError,
    ProviderError,
    number,
    quote_values,
)
from freqtrade.data.hkshare.models import (
    HONG_KONG,
    INDEX_NAMES,
    Instrument,
    instrument_for,
    now_hong_kong,
)


class UnsupportedInstrument(ProviderError):
    pass


def parse_time(value: str) -> datetime:
    fmt = "%Y/%m/%d %H:%M:%S" if "/" in value else "%Y-%m-%d"
    try:
        parsed = datetime.strptime(value, fmt).replace(tzinfo=HONG_KONG)
        if parsed.strftime(fmt) != value:
            raise ValueError("timestamp format mismatch")
        return parsed
    except ValueError as exc:
        raise ProviderError("invalid_timestamp") from exc


def identify(instrument: Instrument, fields: list) -> Instrument:
    if len(fields) < 76 or fields[2] != instrument.symbol.removesuffix(".HK"):
        raise NoDataError("no_data")
    kind = {"GP": "stock", "GP-FUND": "etf", "ZS": "index"}.get(fields[63])
    if kind is None or fields[75] != "HKD" or (kind == "index") != (fields[2] in INDEX_NAMES):
        raise UnsupportedInstrument("unsupported_security_or_currency")
    return instrument.model_copy(update={"name": fields[1] or instrument.name, "kind": kind})


def normalize_candles(rows: list, timeframe: str, instrument: Instrument) -> list[Candle]:
    if not rows:
        raise NoDataError("no_data")
    now = now_hong_kong()
    # The current weekly/monthly row can change its date each trading day.
    # Key by period so an upstream duplicate cannot draw the same period twice.
    result: dict[tuple, Candle] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise ProviderError("invalid_candle")
        stamp = parse_time(str(row[0]))
        op, close, high, low = [number(value) for value in row[1:5]]
        if min(op, close, high, low) <= 0 or high < max(op, close, low) or low > min(op, close):
            raise ProviderError("invalid_ohlc")
        # The index column does not have a verified share/lot definition.
        volume = None if instrument.kind == "index" else number(row[5], optional=True)
        if volume is not None and volume < 0:
            raise ProviderError("negative_volume")
        if timeframe == "1w":
            period, current = stamp.isocalendar()[:2], now.isocalendar()[:2]
        elif timeframe == "1M":
            period, current = (stamp.year, stamp.month), (now.year, now.month)
        else:
            period, current = (stamp.date(),), (now.date(),)
        result[period] = Candle(
            timestamp=int(stamp.timestamp() * 1000),
            open=op,
            close=close,
            high=high,
            low=low,
            volume=volume,
            complete=period < current,
        )
    return sorted(result.values(), key=lambda bar: bar.timestamp)


class TencentHKProvider:
    name = "tencent"

    def __init__(self, http: MarketHTTP):
        self.http = http

    async def snapshots(self, instruments: list[Instrument]) -> dict[str, list]:
        if not instruments:
            return {}
        response = await self.http.get(
            self.name,
            "https://qt.gtimg.cn/q=" + ",".join(item.provider_code for item in instruments),
        )
        return {
            code: values.split("~")
            for code, values in re.findall(r'v_(\w+)="([^"]*)"', response.content.decode("gb18030"))
        }

    async def search(self, query: str) -> list[Instrument]:
        response = await self.http.get(
            self.name, "https://smartbox.gtimg.cn/s3/", {"q": query, "t": "hk"}
        )
        match = re.search(r'v_hint=("[^"\r\n]*")', response.content.decode("gb18030"))
        if not match:
            raise ProviderError("invalid_search_response")
        try:
            hints = json.loads(match.group(1))
        except ValueError as exc:
            raise ProviderError("invalid_search_response") from exc
        items = {}
        for hint in hints.split("^"):
            fields = hint.split("~")
            if len(fields) < 5 or fields[0] != "hk" or fields[4] not in {"GP", "GP-FUND", "ZS"}:
                continue
            try:
                item = instrument_for(fields[1], fields[2])
            except ValueError:
                continue
            items[item.symbol] = item
        # Search labels can call ETFs GP; verify identity, type and currency in
        # one batch instead of guessing from the name or issuing N quote calls.
        snapshots = await self.snapshots(list(items.values())[:20])
        result = []
        for item in list(items.values())[:20]:
            try:
                result.append(identify(item, snapshots.get(item.provider_code, [])))
            except (NoDataError, UnsupportedInstrument):
                continue
        return result

    async def quote(self, instrument: Instrument):
        fields = (await self.snapshots([instrument])).get(instrument.provider_code, [])
        item = identify(instrument, fields)
        quote = quote_values(
            fields[3],
            fields[4],
            fields[5],
            fields[33],
            fields[34],
            None if item.kind == "index" else fields[6],
            None if item.kind == "index" else fields[37],
        )
        return item, quote, parse_time(fields[30]).isoformat()

    async def candles(self, instrument: Instrument, timeframe: str, limit: int):
        field = {"1d": "day", "1w": "week", "1M": "month"}[timeframe]
        code = instrument.provider_code
        response = await self.http.get(
            self.name,
            "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
            {"param": f"{code},{field},,,{limit}"},
        )
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise ProviderError("upstream_error")
        data = payload.get("data", {}).get(code) or {}
        fields = data.get("qt", {}).get(code) or []
        item = identify(instrument, fields)
        bars = normalize_candles(data.get(field) or [], timeframe, item)
        return item, bars[-limit:], parse_time(fields[30]).isoformat()
