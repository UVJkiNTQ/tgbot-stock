import asyncio
import json
import re
import time
from dataclasses import dataclass

import httpx
import yfinance as yf  # type: ignore

import config
from instrument import (
    detect_market,
    market_candidates,
    normalize_symbol,
    parse_symbol,
    resolve_instrument,
)


@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    open: float
    prev_close: float
    high: float
    low: float
    market: str  # "A", "HK", "US", "FUND", "BSE"


MARKET_CURRENCY = {
    "A": "CNY", "HK": "HKD", "US": "USD", "FUND": "CNY", "BSE": "CNY"
}

InstrumentKey = tuple[str, str]


_quote_cache: dict[InstrumentKey, tuple[Quote, float]] = {}
_rate_cache: dict[str, tuple[float, float]] = {}
RATE_CACHE_TTL = 600.0  # 10 min


class RateUnavailableError(RuntimeError):
    pass


def market_currency(symbol: str, market: str | None = None) -> str:
    _, resolved_market = resolve_instrument(symbol, market)
    return MARKET_CURRENCY.get(resolved_market, "CNY")


def quote_currency(quote: Quote) -> str:
    """Derive currency from the provider-confirmed market, not user input."""
    return MARKET_CURRENCY.get(quote.market, "CNY")


def _sina_list(symbol: str, market: str | None = None) -> str:
    s = normalize_symbol(symbol)
    _, m = resolve_instrument(s, market)
    if m == "HK":
        return f"hk{s.zfill(5)}"
    if m == "BSE":
        return f"bj{s.zfill(6)}"
    if m == "A":
        if s[0] in ("5", "6", "9"):
            return f"sh{s}"
        return f"sz{s}"
    raise ValueError(f"Cannot build sina code for {symbol}")


async def _fetch_funds(symbols: list[str]) -> dict[str, Quote]:
    """Fetch fund NAV from Eastmoney's pingzhongdata (unit net value history)."""
    results: dict[str, Quote] = {}
    async with httpx.AsyncClient(
        timeout=10, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com"}
    ) as client:
        for sym in symbols:
            try:
                resp = await client.get(
                    f"https://fund.eastmoney.com/pingzhongdata/{sym}.js"
                )
                resp.raise_for_status()
                text = resp.content.decode("utf-8", errors="replace")

                name_m = re.search(r'fS_name\s*=\s*"([^"]*)"', text)
                trend_m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text)
                if not name_m or not trend_m:
                    continue
                name = name_m.group(1).strip()
                trend = json.loads(trend_m.group(1))
                if not trend:
                    continue

                last = trend[-1]
                prev = trend[-2] if len(trend) >= 2 else last
                price = float(last.get("y") or 0)
                prev_close = float(prev.get("y") or 0)
                if not name or price <= 0:
                    continue
                results[sym] = Quote(
                    symbol=sym,
                    name=name,
                    price=price,
                    open=prev_close,
                    prev_close=prev_close,
                    high=max(price, prev_close),
                    low=min(price, prev_close),
                    market="FUND",
                )
            except Exception:
                continue
    return results


def _parse_sina_fields(
    symbol: str, fields: list[str], market: str | None = None
) -> Quote | None:
    """Parse one Sina quote; A-share and HK payload layouts differ."""
    _, resolved_market = resolve_instrument(symbol, market)
    try:
        if resolved_market == "HK":
            # HK: English name, Chinese name, open, previous close, high,
            # low, current price, ...
            if len(fields) < 7 or not fields[6]:
                return None
            name = fields[1] or fields[0]
            open_price, prev_close = fields[2], fields[3]
            high, low, price = fields[4], fields[5], fields[6]
        else:
            # A share: name, open, previous close, current, high, low, ...
            if len(fields) < 6 or not fields[3]:
                return None
            name = fields[0]
            open_price, prev_close, price = fields[1], fields[2], fields[3]
            high, low = fields[4], fields[5]
        return Quote(
            symbol=symbol,
            name=name,
            price=float(price),
            open=float(open_price) if open_price else 0.0,
            prev_close=float(prev_close) if prev_close else 0.0,
            high=float(high) if high else 0.0,
            low=float(low) if low else 0.0,
            market=resolved_market,
        )
    except (ValueError, IndexError):
        return None


async def _fetch_sina(
    symbols: list[str], market: str | None = None
) -> dict[str, Quote]:
    results: dict[str, Quote] = {}
    sina_codes = []
    code_map: dict[str, str] = {}
    for sym in symbols:
        try:
            sc = _sina_list(sym, market)
            sina_codes.append(sc)
            code_map[sc] = sym
        except ValueError:
            continue

    if not sina_codes:
        return results

    url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url, headers={"Referer": "https://finance.sina.com.cn"}
            )
            resp.raise_for_status()
    except httpx.HTTPError:
        return results

    # Sina does not consistently send a charset header.  Its payload is GBK;
    # relying on httpx's default decoding turns Chinese names into mojibake.
    text = resp.content.decode("gb18030", errors="replace")
    for sc in sina_codes:
        prefix = f'var hq_str_{sc}="'
        start = text.find(prefix)
        if start == -1:
            continue
        start += len(prefix)
        end = text.find('"', start)
        if end == -1:
            continue
        sym = code_map[sc]
        fields = text[start:end].split(",")
        quote = _parse_sina_fields(sym, fields, market)
        if quote is not None:
            results[sym] = quote
    return results


async def _fetch_us(symbols: list[str]) -> dict[str, Quote]:
    def fetch_one(sym: str) -> Quote | None:
        try:
            ticker = yf.Ticker(sym)
            info = ticker.fast_info
            price = (
                info.get("lastPrice") or info.get("regularMarketPrice") or 0.0
            )
            prev_close = (
                info.get("previousClose")
                or info.get("regularMarketPreviousClose")
                or 0.0
            )
            return Quote(
                symbol=sym,
                name=ticker.ticker,
                price=float(price),
                open=float(info.get("open") or 0.0),
                prev_close=float(prev_close),
                high=float(info.get("dayHigh") or 0.0),
                low=float(info.get("dayLow") or 0.0),
                market="US",
            )
        except Exception:
            return None

    results: dict[str, Quote] = {}
    fetched = await asyncio.gather(
        *(asyncio.to_thread(fetch_one, sym) for sym in symbols)
    )
    for sym, quote in zip(symbols, fetched):
        if quote is not None:
            results[sym] = quote
    return results


async def get_quote(symbol: str, market: str | None = None) -> Quote | None:
    key = resolve_instrument(symbol, market)
    fetched = await get_quotes([key])
    return fetched.get(key)


async def get_quotes(
    instruments: list[str | InstrumentKey],
) -> dict[InstrumentKey, Quote]:
    results: dict[InstrumentKey, Quote] = {}
    now = time.time()

    a_syms: list[str] = []
    hk_syms: list[str] = []
    us_syms: list[str] = []
    fund_syms: list[str] = []
    bse_syms: list[str] = []

    keys = list(
        dict.fromkeys(
            resolve_instrument(item[0], item[1])
            if isinstance(item, tuple)
            else resolve_instrument(item)
            for item in instruments
        )
    )
    for base, market in keys:
        key = (base, market)
        cached = _quote_cache.get(key)
        if cached and now - cached[1] < config.QUOTE_CACHE_TTL:
            results[key] = cached[0]
            continue
        if market == "FUND":
            fund_syms.append(base)
        elif market == "BSE":
            bse_syms.append(base)
        elif market == "US":
            us_syms.append(base)
        elif market == "HK":
            hk_syms.append(base)
        else:
            a_syms.append(base)

    missed_a_syms: list[str] = []

    for market, symbols in (
        ("A", a_syms),
        ("HK", hk_syms),
        ("BSE", bse_syms),
    ):
        if not symbols:
            continue
        sina_results = await _fetch_sina(symbols, market)
        now2 = time.time()
        for sym, q in sina_results.items():
            key = (sym, market)
            _quote_cache[key] = (q, now2)
            results[key] = q
        if market == "A":
            for sym in symbols:
                if sym not in sina_results:
                    missed_a_syms.append(sym)

    if missed_a_syms or fund_syms:
        fund_results = await _fetch_funds(
            list(dict.fromkeys(missed_a_syms + fund_syms))
        )
        now2 = time.time()
        for sym, q in fund_results.items():
            key = (sym, "FUND")
            _quote_cache[key] = (q, now2)
            # A same-code fund discovered while probing a missing A quote is
            # cached under its own identity, never returned as the A asset.
            if key in keys:
                results[key] = q

    if us_syms:
        us_results = await _fetch_us(us_syms)
        now2 = time.time()
        for sym, q in us_results.items():
            key = (sym, "US")
            _quote_cache[key] = (q, now2)
            results[key] = q

    return results


async def get_rate(currency: str) -> float:
    """Fetch exchange rate to CNY. Returns 1.0 for CNY."""
    currency = currency.strip().upper()
    if currency == "CNY":
        return 1.0

    now = time.time()
    cached = _rate_cache.get(currency)
    if cached and now - cached[1] < RATE_CACHE_TTL:
        return cached[0]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://open.er-api.com/v6/latest/{currency}"
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("result") != "success" or data.get("base_code") != currency:
                raise ValueError(f"invalid exchange-rate response for {currency}")
            rate = float(data["rates"]["CNY"])
            if rate <= 0:
                raise ValueError(f"invalid exchange rate for {currency}")
    except Exception as exc:
        if cached:
            return cached[0]
        raise RateUnavailableError(f"unable to fetch {currency}/CNY rate") from exc
    _rate_cache[currency] = (rate, now)
    return rate


def clear_cache() -> None:
    _quote_cache.clear()
    _rate_cache.clear()
