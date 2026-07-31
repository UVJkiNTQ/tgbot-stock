import asyncio
import json
import time
from dataclasses import dataclass

import httpx
import yfinance as yf  # type: ignore

import config


@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    open: float
    prev_close: float
    high: float
    low: float
    market: str  # "A", "HK", "US", "FUND"


MARKET_CURRENCY = {"A": "CNY", "HK": "HKD", "US": "USD", "FUND": "CNY"}

_quote_cache: dict[str, tuple[Quote, float]] = {}
_rate_cache: dict[str, tuple[float, float]] = {}
RATE_CACHE_TTL = 600.0  # 10 min


class RateUnavailableError(RuntimeError):
    pass


def normalize_symbol(symbol: str) -> str:
    """Return the canonical symbol used by quote providers and the database."""
    s = symbol.strip().upper()
    if not s:
        raise ValueError("symbol cannot be empty")

    if s.endswith(".HK") and s[:-3].isdigit():
        s = s[:-3]
    elif s.startswith("HK") and s[2:].isdigit():
        s = s[2:]

    # Hong Kong codes are conventionally five digits, but users commonly omit
    # their leading zeroes (700 / 0700 instead of 00700).
    if s.isdigit() and len(s) <= 5:
        return s.zfill(5)
    return s


def detect_market(symbol: str) -> str:
    s = normalize_symbol(symbol)
    if s.isdigit():
        if len(s) == 5:
            return "HK"
        if len(s) == 6:
            first = s[0]
            if first in ("5", "6", "9"):
                return "A"
            return "A"
    return "US"


def market_currency(symbol: str) -> str:
    return MARKET_CURRENCY.get(detect_market(symbol), "CNY")


def quote_currency(quote: Quote) -> str:
    """Derive currency from the provider-confirmed market, not user input."""
    return MARKET_CURRENCY.get(quote.market, "CNY")


def _sina_list(symbol: str) -> str:
    s = normalize_symbol(symbol)
    m = detect_market(s)
    if m == "HK":
        return f"hk{s.zfill(5)}"
    if m == "A":
        if s[0] in ("5", "6", "9"):
            return f"sh{s}"
        return f"sz{s}"
    raise ValueError(f"Cannot build sina code for {symbol}")


async def _fetch_funds(symbols: list[str]) -> dict[str, Quote]:
    results: dict[str, Quote] = {}
    async with httpx.AsyncClient(timeout=10) as client:
        for sym in symbols:
            try:
                resp = await client.get(
                    f"https://fundgz.1234567.com.cn/js/{sym}.js",
                    headers={"Referer": "https://fund.eastmoney.com"},
                )
                resp.raise_for_status()
                raw = resp.content
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("gbk", errors="replace")
                prefix = "jsonpgz("
                start = text.find(prefix)
                if start == -1:
                    continue
                start += len(prefix)
                end = text.rfind(")")
                if end == -1:
                    continue
                data = json.loads(text[start:end])
                name = data.get("name", "")
                gsz = float(data.get("gsz", 0) or 0)
                dwjz = float(data.get("dwjz", 0) or 0)
                gztime = data.get("gztime", "")
                if not name or gsz <= 0:
                    continue
                results[sym] = Quote(
                    symbol=sym,
                    name=name,
                    price=gsz,
                    open=dwjz,
                    prev_close=dwjz,
                    high=gsz,
                    low=gsz,
                    market="FUND",
                )
            except Exception:
                continue
    return results


def _parse_sina_fields(symbol: str, fields: list[str]) -> Quote | None:
    """Parse one Sina quote; A-share and HK payload layouts differ."""
    market = detect_market(symbol)
    try:
        if market == "HK":
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
            market=market,
        )
    except (ValueError, IndexError):
        return None


async def _fetch_sina(symbols: list[str]) -> dict[str, Quote]:
    results: dict[str, Quote] = {}
    sina_codes = []
    code_map: dict[str, str] = {}
    for sym in symbols:
        try:
            sc = _sina_list(sym)
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
        quote = _parse_sina_fields(sym, fields)
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


async def get_quote(symbol: str) -> Quote | None:
    symbols = [normalize_symbol(symbol)]
    quotes = await get_quotes(symbols)
    return quotes.get(symbols[0])


async def get_quotes(symbols: list[str]) -> dict[str, Quote]:
    syms = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    results: dict[str, Quote] = {}
    now = time.time()

    sina_syms: list[str] = []
    us_syms: list[str] = []

    for sym in syms:
        cached = _quote_cache.get(sym)
        if cached and now - cached[1] < config.QUOTE_CACHE_TTL:
            results[sym] = cached[0]
            continue
        m = detect_market(sym)
        if m == "US":
            us_syms.append(sym)
        else:
            sina_syms.append(sym)

    missed_a_syms: list[str] = []

    if sina_syms:
        sina_results = await _fetch_sina(sina_syms)
        now2 = time.time()
        for sym, q in sina_results.items():
            _quote_cache[sym] = (q, now2)
            results[sym] = q
        for sym in sina_syms:
            if sym not in sina_results and detect_market(sym) == "A":
                missed_a_syms.append(sym)

    if missed_a_syms:
        fund_results = await _fetch_funds(missed_a_syms)
        now2 = time.time()
        for sym, q in fund_results.items():
            _quote_cache[sym] = (q, now2)
            results[sym] = q

    if us_syms:
        us_results = await _fetch_us(us_syms)
        now2 = time.time()
        for sym, q in us_results.items():
            _quote_cache[sym] = (q, now2)
            results[sym] = q

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
