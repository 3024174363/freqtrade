# ruff: noqa: RUF001
import re
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from freqtrade.data.ashare.models import Candle, Quote


HONG_KONG = ZoneInfo("Asia/Hong_Kong")
Timeframe = Literal["1d", "1w", "1M"]
TIMEFRAMES: tuple[Timeframe, ...] = ("1d", "1w", "1M")
INDEX_NAMES = {"HSI": "恒生指数", "HSTECH": "恒生科技指数"}


class Instrument(BaseModel):
    symbol: str
    name: str
    exchange: Literal["HK"] = "HK"
    kind: Literal["stock", "index", "etf"] = "stock"
    currency: Literal["HKD"] = "HKD"

    @property
    def provider_code(self) -> str:
        return "hk" + self.symbol.removesuffix(".HK")

    @property
    def volume_unit(self) -> str:
        return {"stock": "shares", "etf": "fund_units", "index": "unavailable"}[self.kind]


def instrument_for(symbol: str, name: str = "") -> Instrument:
    code = symbol.strip().upper().removesuffix(".HK")
    if code.startswith("HK."):
        code = code[3:]
    elif code.startswith("HK"):
        code = code[2:]
    if code in INDEX_NAMES:
        return Instrument(symbol=f"{code}.HK", name=name or INDEX_NAMES[code], kind="index")
    if not re.fullmatch(r"[0-9]{1,5}", code) or int(code) == 0:
        raise ValueError("请输入港股代码，如 hk00700、02800.HK、HSI.HK 或 HSTECH.HK")
    code = code.zfill(5)
    return Instrument(symbol=f"{code}.HK", name=name or f"{code}.HK")


class Metadata(BaseModel):
    source: Literal["tencent"] = "tencent"
    requested_source: Literal["tencent"] = "tencent"
    fallback_used: bool = False
    currency: Literal["HKD"] = "HKD"
    timezone: Literal["Asia/Hong_Kong"] = "Asia/Hong_Kong"
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


def now_hong_kong() -> datetime:
    return datetime.now(HONG_KONG)


def session_label(now: datetime) -> str:
    """A clock schedule only, without claiming a holiday is an open trading day."""
    if now.weekday() >= 5:
        return "周末 · 显示最近行情"
    minute = now.hour * 60 + now.minute
    if 720 <= minute < 780:
        return "午间休市时段"
    if 570 <= minute < 720 or 780 <= minute < 960:
        return "连续交易时间范围 · 请核对交易日及行情时间"
    return "非连续交易时段 · 显示最近行情"
