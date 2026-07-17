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


def detect_market(symbol: str) -> str:
    s = symbol.upper()
    if s.isalpha():
        return "US"
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


def _sina_list(symbol: str) -> str:
    s = symbol.upper()
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
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            url, headers={"Referer": "https://finance.sina.com.cn"}
        )
        resp.raise_for_status()

    text = resp.text
    for sc in sina_codes:
        prefix = f'var hq_str_{sc}="'
        start = text.find(prefix)
        if start == -1:
            continue
        start += len(prefix)
        end = text.find('"', start)
        if end == -1:
            continue
        fields = text[start:end].split(",")
        if len(fields) < 4 or not fields[3]:
            continue
        sym = code_map[sc]
        m = detect_market(sym)
        results[sym] = Quote(
            symbol=sym,
            name=fields[0],
            price=float(fields[3]),
            open=float(fields[1]) if fields[1] else 0.0,
            prev_close=float(fields[2]) if fields[2] else 0.0,
            high=float(fields[4]) if fields[4] else 0.0,
            low=float(fields[5]) if fields[5] else 0.0,
            market=m,
        )
    return results


async def _fetch_us(symbols: list[str]) -> dict[str, Quote]:
    results: dict[str, Quote] = {}
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            info = ticker.fast_info
            price = info.get("lastPrice") or info.get("regularMarketPrice") or 0.0
            prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose") or 0.0
            results[sym] = Quote(
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
            continue
    return results


async def get_quote(symbol: str) -> Quote | None:
    symbols = [symbol]
    quotes = await get_quotes(symbols)
    return quotes.get(symbol.upper())


async def get_quotes(symbols: list[str]) -> dict[str, Quote]:
    syms = [s.upper() for s in symbols]
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
            rate = float(data["rates"]["CNY"])
    except Exception:
        rate = cached[0] if cached else 1.0
    _rate_cache[currency] = (rate, now)
    return rate


def clear_cache() -> None:
    _quote_cache.clear()
    _rate_cache.clear()
