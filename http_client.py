import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}
_YAHOO_CHART_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)

_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None

_K = TypeVar("_K")
_V = TypeVar("_V")


def get_client() -> httpx.AsyncClient:
    """Return a shared ``httpx.AsyncClient`` bound to the current event loop."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is not None and not _client.is_closed and _client_loop is loop:
        return _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=10.0),
        headers=_DEFAULT_HEADERS,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True,
    )
    _client_loop = loop
    return _client


async def close_client() -> None:
    """Close the shared HTTP client if one is open on the current loop."""
    global _client, _client_loop
    client = _client
    loop = _client_loop
    _client = None
    _client_loop = None
    if client is not None and not client.is_closed:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or loop is running:
            await client.aclose()


def cache_set(
    cache: dict[_K, tuple[_V, float]],
    key: _K,
    value: _V,
    now: float,
    *,
    ttl: float,
    max_size: int,
) -> None:
    """Insert into a TTL dict cache while purging expired and oldest entries."""
    cache.pop(key, None)
    cache[key] = (value, now)
    if len(cache) <= max_size:
        return
    expired = [k for k, (_v, ts) in cache.items() if now - ts >= ttl]
    for k in expired:
        cache.pop(k, None)
    while len(cache) > max_size:
        cache.pop(next(iter(cache)))


def bounded_set(
    cache: dict[_K, _V], key: _K, value: _V, *, max_size: int
) -> None:
    """Insert into a plain dict while keeping its size within ``max_size``."""
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > max_size:
        cache.pop(next(iter(cache)))


def yahoo_exchange_tz(meta: dict[str, Any]) -> timezone | ZoneInfo:
    """Resolve the exchange timezone from Yahoo chart metadata."""
    tz_name = meta.get("exchangeTimezoneName")
    if isinstance(tz_name, str) and tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            pass
    offset = meta.get("gmtoffset")
    if isinstance(offset, (int, float)):
        return timezone(timedelta(seconds=int(offset)))
    return timezone.utc


def yahoo_timestamp_to_local(
    ts: int | float, tz: timezone | ZoneInfo
) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz)


async def fetch_yahoo_chart(
    ticker: str,
    *,
    range_: str,
    interval: str,
    include_prepost: bool = False,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Fetch a Yahoo Finance v8 chart result node via the shared HTTP client."""
    params = {
        "range": range_,
        "interval": interval,
        "includePrePost": "true" if include_prepost else "false",
    }
    client = get_client()
    last_exc: Exception | None = None
    for host in _YAHOO_CHART_HOSTS:
        url = f"{host}/v8/finance/chart/{ticker}"
        try:
            resp = await client.get(url, params=params, timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            last_exc = exc
            continue

        chart = payload.get("chart") if isinstance(payload, dict) else None
        results = chart.get("result") if isinstance(chart, dict) else None
        if isinstance(results, list) and results and isinstance(results[0], dict):
            return results[0]
        return None

    if last_exc is not None:
        logger.debug("Yahoo chart fetch failed for %s: %s", ticker, last_exc)
    return None
