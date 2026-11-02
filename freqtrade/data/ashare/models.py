# ruff: noqa: RUF001
import re
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel


SHANGHAI = ZoneInfo("Asia/Shanghai")
Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1M"]
Source = Literal["auto", "tencent", "sina", "eastmoney"]
TIMEFRAMES: tuple[Timeframe, ...] = ("1m", "5m", "15m", "30m", "1h", "1d", "1w", "1M")
INTRADAY_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


class Instrument(BaseModel):
    symbol: str
    name: str
    exchange: Literal["SH", "SZ"]
    kind: Literal["stock", "index", "etf"]

    @property
    def provider_code(self) -> str:
        return self.exchange.lower() + self.symbol[:6]

    @property
    def volume_unit(self) -> str:
        return {"stock": "shares", "etf": "fund_units", "index": "index_lots"}[self.kind]


def instrument_for(symbol: str, name: str = "") -> Instrument:
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol):
        raise ValueError("请使用带交易所的代码，例如 600519.SH 或 000001.SZ；暂不支持北交所")
    code, exchange = symbol.split(".")
    if (exchange == "SH" and code.startswith("000")) or (
        exchange == "SZ" and code.startswith("399")
    ):
        kind = "index"
    elif (exchange == "SH" and code.startswith(("51", "52", "56", "58"))) or (
        exchange == "SZ" and code.startswith("15")
    ):
        kind = "etf"
    elif (exchange == "SH" and code.startswith(("600", "601", "603", "605", "688", "689"))) or (
        exchange == "SZ" and code.startswith(("000", "001", "002", "003", "300", "301"))
    ):
        kind = "stock"
    else:
        raise ValueError("当前仅支持沪深 A 股、主要指数和 ETF，暂不支持该代码类型")
    return Instrument(symbol=symbol, name=name or symbol, exchange=exchange, kind=kind)


class Quote(BaseModel):
    price: float | None
    previous_close: float | None
    open: float | None
    high: float | None
    low: float | None
    change: float | None
    change_percent: float | None
    volume: float | None
    amount: float | None


class Candle(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    complete: bool


class Metadata(BaseModel):
    source: str
    requested_source: Source = "auto"
    fallback_used: bool = False
    volume_unit: str
    volume_note: str | None = None
    adjustment: Literal["raw"] = "raw"
    data_time: str | None = None
    fetched_at: str
    cached: bool = False
    stale: bool = False
    warning: str | None = None
    session: str
    timestamp_convention: str = "quote_time"


class QuoteResponse(BaseModel):
    instrument: Instrument
    quote: Quote
    meta: Metadata


class CandlesResponse(BaseModel):
    instrument: Instrument
    timeframe: Timeframe
    candles: list[Candle]
    meta: Metadata
    coverage_start: int
    coverage_end: int
    requested_count: int
    complete: bool


class SearchResponse(BaseModel):
    items: list[Instrument]
    source: str
    cached: bool = False
    stale: bool = False
    warning: str | None = None


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def session_label(now: datetime) -> str:
    """Time-of-day only: without an exchange calendar do not assert an open day."""
    if now.weekday() >= 5:
        return "周末 · 显示最近行情"
    minute = now.hour * 60 + now.minute
    if 690 <= minute < 780:
        return "午间休市时段"
    if 570 <= minute < 690 or 780 <= minute < 900:
        return "交易时段 · 以行情时间为准"
    return "非连续交易时段 · 显示最近行情"
