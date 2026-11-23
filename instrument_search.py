import json
import logging
import re
import time
from dataclasses import dataclass

from http_client import cache_set, get_client
from instrument import qualified_symbol


SEARCH_CACHE_TTL = 300.0
SEARCH_CACHE_MAX = 256
MAX_QUERY_LENGTH = 40
_YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
_TENCENT_SEARCH_URL = "https://smartbox.gtimg.cn/s3/"
_US_EXCHANGES = frozenset(
    {
        "NYQ", "NMS", "NGM", "NCM", "PCX", "ASE",
        "PNK", "OTC", "OQX", "BTS", "NAS", "NYS", "NYE",
    }
)
_LOCAL_EXCHANGES = frozenset({"SHH", "SHZ", "HKG", "JPX"})
_OTC_EXCHANGES = frozenset({"PNK", "OTC", "OQX"})


class SearchUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchResult:
    symbol: str
    market: str
    name: str
    exchange: str = ""
    rank: int = 0
    quote_type: str = "EQUITY"

    def display_symbol(self) -> str:
        return qualified_symbol(self.symbol, self.market)


_search_cache: dict[tuple[str, int], tuple[list[SearchResult], float]] = {}


def _has_cjk(text: str) -> bool:
    return any(ord(char) > 127 for char in text)


def _yahoo_result(quote: dict) -> SearchResult | None:
    quote_type = str(quote.get("quoteType") or "").upper()
    if quote_type not in {"EQUITY", "ETF"}:
        return None

    raw_symbol = str(quote.get("symbol") or "").strip().upper()
    exchange = str(quote.get("exchange") or "").strip().upper()
    exchange_name = str(quote.get("exchDisp") or "").strip()
    name = str(
        quote.get("longname")
        or quote.get("shortname")
        or raw_symbol
    ).strip()
    if not raw_symbol or not name:
        return None

    if raw_symbol.endswith(".T"):
        symbol, market = raw_symbol[:-2], "JP"
    elif raw_symbol.endswith(".HK"):
        symbol, market = raw_symbol[:-3].zfill(5), "HK"
    elif raw_symbol.endswith(".SS"):
        symbol, market = raw_symbol[:-3], "A"
    elif raw_symbol.endswith(".SZ"):
        symbol, market = raw_symbol[:-3], "A"
    elif raw_symbol.endswith(".BJ"):
        symbol, market = raw_symbol[:-3], "BSE"
    elif exchange in _US_EXCHANGES:
        symbol, market = raw_symbol, "US"
    else:
        return None

    if not symbol:
        return None
    if market in {"A", "BSE"} and not (
        symbol.isdigit() and len(symbol) == 6
    ):
        return None
    if market == "HK" and not symbol.isdigit():
        return None
    if market == "JP" and not symbol.replace("A", "").isdigit():
        return None

    if exchange in _LOCAL_EXCHANGES:
        rank = 0
    elif exchange in _OTC_EXCHANGES:
        rank = 2
    elif exchange in _US_EXCHANGES:
        rank = 1
    else:
        rank = 3
    return SearchResult(
        symbol=symbol,
        market=market,
        name=name,
        exchange=exchange_name,
        rank=rank,
        quote_type=quote_type,
    )


async def _yahoo_search(query: str) -> list[SearchResult]:
    params = {
        "q": query,
        "quotesCount": "20",
        "newsCount": "0",
        "listsCount": "0",
    }
    client = get_client()
    response = await client.get(_YAHOO_SEARCH_URL, params=params, timeout=8)
    response.raise_for_status()
    payload = response.json()

    results: list[SearchResult] = []
    for quote in payload.get("quotes") or []:
        result = _yahoo_result(quote)
        if result is not None:
            results.append(result)
    return results


def _parse_tencent_hint(text: str) -> list[SearchResult]:
    match = re.search(r'v_hint="(.*)"\s*;?\s*$', text, re.S)
    if match is None:
        return []
    body = match.group(1)
    if body in ("", "N"):
        return []
    try:
        body = json.loads(f'"{body}"')
    except json.JSONDecodeError:
        try:
            body = body.encode("latin-1").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return []

    results: list[SearchResult] = []
    for entry in body.split("^"):
        fields = entry.split("~")
        if len(fields) < 5:
            continue
        raw_market, raw_code, name, _pinyin, kind = fields[:5]
        if not kind.upper().startswith("GP"):
            continue
        code = raw_code.strip()
        if not code:
            continue

        if raw_market in {"sh", "sz"}:
            market = "A"
            exchange = "沪深"
            rank = 0
        elif raw_market == "bj":
            market = "BSE"
            exchange = "北交所"
            rank = 0
        elif raw_market == "hk":
            market = "HK"
            exchange = "港股"
            rank = 0
            code = code.zfill(5)
            if code.startswith("8"):
                continue
        elif raw_market == "us":
            market = "US"
            exchange = "美股"
            rank = 1
            code = code.split(".", 1)[0].upper()
        else:
            continue

        if market in {"A", "BSE"} and not (
            code.isdigit() and len(code) == 6
        ):
            continue
        if market == "HK" and not code.isdigit():
            continue
        results.append(
            SearchResult(
                symbol=code,
                market=market,
                name=name.strip() or code,
                exchange=exchange,
                rank=rank,
            )
        )
    return results


async def _tencent_search(query: str) -> list[SearchResult]:
    client = get_client()
    response = await client.get(
        _TENCENT_SEARCH_URL,
        params={"q": query, "t": "all"},
        headers={"Referer": "https://gu.qq.com/"},
        timeout=8,
    )
    response.raise_for_status()
    return _parse_tencent_hint(response.text)


def _finalize(results: list[SearchResult], limit: int) -> list[SearchResult]:
    ordered = sorted(
        enumerate(results),
        key=lambda item: (
            item[1].rank,
            0 if item[1].quote_type == "EQUITY" else 1,
            item[0],
        ),
    )
    if any(result.quote_type == "EQUITY" for _, result in ordered):
        ordered = [
            item for item in ordered if item[1].quote_type == "EQUITY"
        ]
    selected: list[SearchResult] = []
    seen_symbols: set[tuple[str, str]] = set()
    seen_names: set[tuple[str, str]] = set()
    for _, result in ordered:
        symbol_key = (result.symbol, result.market)
        name_key = (result.name.casefold(), result.market)
        if symbol_key in seen_symbols or name_key in seen_names:
            continue
        seen_symbols.add(symbol_key)
        seen_names.add(name_key)
        selected.append(result)
        if len(selected) >= limit:
            break
    return selected


def _should_expand(query: str, results: list[SearchResult]) -> bool:
    return (
        not _has_cjk(query)
        and " " not in query
        and not query.isdigit()
        and not any(result.quote_type == "EQUITY" for result in results)
    )


async def search_instruments(
    query: str, limit: int = 5
) -> list[SearchResult]:
    query = query.strip()
    if not query or len(query) > MAX_QUERY_LENGTH:
        return []
    limit = max(1, min(limit, 10))
    cache_key = (query.casefold(), limit)
    cached = _search_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[1] < SEARCH_CACHE_TTL:
        return list(cached[0])

    providers = (
        (("tencent", _tencent_search),)
        if _has_cjk(query)
        else (("yahoo", _yahoo_search), ("tencent", _tencent_search))
    )
    results: list[SearchResult] = []
    failures: list[Exception] = []
    for name, provider in providers:
        try:
            results = await provider(query)
        except Exception as exc:
            failures.append(exc)
            logging.getLogger(__name__).warning(
                "instrument search provider %s failed",
                name,
                exc_info=True,
            )
            continue
        if name == "yahoo" and _should_expand(query, results):
            try:
                results.extend(await _yahoo_search(f"{query} corp"))
            except Exception:
                logging.getLogger(__name__).warning(
                    "instrument search expansion failed for %s",
                    query,
                    exc_info=True,
                )
        if results:
            break
    if not results and len(failures) == len(providers):
        raise SearchUnavailableError("all instrument search providers failed")

    final = _finalize(results, limit)
    cache_set(
        _search_cache,
        cache_key,
        final,
        now,
        ttl=SEARCH_CACHE_TTL,
        max_size=SEARCH_CACHE_MAX,
    )
    return list(final)


def clear_cache() -> None:
    _search_cache.clear()
