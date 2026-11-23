import asyncio
import json
import math
import re
import time
from dataclasses import dataclass

import httpx

import config
from http_client import cache_set, fetch_yahoo_chart, get_client
from instrument import (
    detect_market,
    market_candidates,
    normalize_symbol,
    parse_symbol,
    resolve_instrument,
    yahoo_symbol,
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
    market: str  # "A", "HK", "US", "FUND", "BSE", "JP"


MARKET_CURRENCY = {
    "A": "CNY",
    "HK": "HKD",
    "US": "USD",
    "FUND": "CNY",
    "BSE": "CNY",
    "JP": "JPY",
}

InstrumentKey = tuple[str, str]


_quote_cache: dict[InstrumentKey, tuple[Quote, float]] = {}
_rate_cache: dict[str, tuple[float, float]] = {}
RATE_CACHE_TTL = 600.0  # 10 min
_QUOTE_CACHE_MAX = 512
_RATE_CACHE_MAX = 64


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
    client = get_client()
    headers = {"Referer": "https://fund.eastmoney.com"}
    for sym in symbols:
        try:
            resp = await client.get(
                f"https://fund.eastmoney.com/pingzhongdata/{sym}.js",
                headers=headers,
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
        client = get_client()
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


def _first_finite(values: list | None) -> float:
    if not isinstance(values, list):
        return 0.0
    for item in reversed(values):
        if item is None:
            continue
        try:
            num = float(item)
        except (TypeError, ValueError):
            continue
        if math.isfinite(num) and num > 0:
            return num
    return 0.0


def _parse_yahoo_chart_quote(
    sym: str, market: str, result: dict
) -> Quote | None:
    meta = result.get("meta")
    if not isinstance(meta, dict):
        return None
    price = float(meta.get("regularMarketPrice") or 0.0)
    if not math.isfinite(price) or price <= 0:
        return None

    prev_close = float(
        meta.get("chartPreviousClose")
        or meta.get("previousClose")
        or 0.0
    )
    indicators = result.get("indicators") or {}
    quote_nodes = indicators.get("quote") or []
    quote_node = (
        quote_nodes[0]
        if isinstance(quote_nodes, list) and quote_nodes and isinstance(quote_nodes[0], dict)
        else {}
    )
    open_price = float(
        meta.get("regularMarketOpen")
        or _first_finite(quote_node.get("open"))
        or 0.0
    )
    high = float(
        meta.get("regularMarketDayHigh")
        or _first_finite(quote_node.get("high"))
        or 0.0
    )
    low = float(
        meta.get("regularMarketDayLow")
        or _first_finite(quote_node.get("low"))
        or 0.0
    )
    if market == "JP":
        name = str(
            meta.get("longName") or meta.get("shortName") or sym
        ).strip()
    else:
        name = str(
            meta.get("shortName")
            or meta.get("longName")
            or meta.get("symbol")
            or yahoo_symbol(sym, market)
        ).strip()

    return Quote(
        symbol=sym,
        name=name or sym,
        price=price,
        open=open_price,
        prev_close=prev_close,
        high=high,
        low=low,
        market=market,
    )


async def _fetch_yahoo(
    symbols: list[str], market: str = "US"
) -> dict[str, Quote]:
    async def fetch_one(sym: str) -> Quote | None:
        try:
            result = await fetch_yahoo_chart(
                yahoo_symbol(sym, market), range_="1d", interval="1d"
            )
            if result is None:
                return None
            return _parse_yahoo_chart_quote(sym, market, result)
        except Exception:
            return None

    results: dict[str, Quote] = {}
    fetched = await asyncio.gather(*(fetch_one(sym) for sym in symbols))
    for sym, quote in zip(symbols, fetched):
        if quote is not None:
            results[sym] = quote
    return results


def japan_alternative_code(symbol: str) -> str | None:
    """Return a Japanese code for an unresolved four-digit HK-style symbol.

    A bare four-digit code is historically treated as Hong Kong and padded to
    five digits.  When that quote is missing, deriving the original four-digit
    code lets callers offer ``7203.JP`` as a disambiguation without changing
    the default meaning of ``7203``.
    """
    base, forced = parse_symbol(symbol)
    if forced is not None:
        return None
    if (
        len(base) == 5
        and base.startswith("0")
        and base[1] != "0"
        and base.isdigit()
    ):
        return base[1:]
    return None


async def get_japan_alternative(symbol: str) -> Quote | None:
    code = japan_alternative_code(symbol)
    if code is None:
        return None
    return await get_quote(f"{code}.JP")


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
    jp_syms: list[str] = []
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
        elif market == "JP":
            jp_syms.append(base)
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
            cache_set(
                _quote_cache,
                key,
                q,
                now2,
                ttl=config.QUOTE_CACHE_TTL,
                max_size=_QUOTE_CACHE_MAX,
            )
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
            cache_set(
                _quote_cache,
                key,
                q,
                now2,
                ttl=config.QUOTE_CACHE_TTL,
                max_size=_QUOTE_CACHE_MAX,
            )
            # A same-code fund discovered while probing a missing A quote is
            # cached under its own identity, never returned as the A asset.
            if key in keys:
                results[key] = q

    if us_syms:
        us_results = await _fetch_yahoo(us_syms, "US")
        now2 = time.time()
        for sym, q in us_results.items():
            key = (sym, "US")
            cache_set(
                _quote_cache,
                key,
                q,
                now2,
                ttl=config.QUOTE_CACHE_TTL,
                max_size=_QUOTE_CACHE_MAX,
            )
            results[key] = q

    if jp_syms:
        jp_results = await _fetch_yahoo(jp_syms, "JP")
        now2 = time.time()
        for sym, q in jp_results.items():
            key = (sym, "JP")
            cache_set(
                _quote_cache,
                key,
                q,
                now2,
                ttl=config.QUOTE_CACHE_TTL,
                max_size=_QUOTE_CACHE_MAX,
            )
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
        client = get_client()
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
    cache_set(
        _rate_cache,
        currency,
        rate,
        now,
        ttl=RATE_CACHE_TTL,
        max_size=_RATE_CACHE_MAX,
    )
    return rate


def clear_cache() -> None:
    _quote_cache.clear()
    _rate_cache.clear()
