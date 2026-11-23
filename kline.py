import io
import math
import time
from dataclasses import dataclass
from datetime import date, datetime

from PIL import Image, ImageDraw

from chartstyle import fit_text, font as load_font, font_path
from http_client import (
    cache_set,
    fetch_yahoo_chart,
    get_client,
    yahoo_exchange_tz,
    yahoo_timestamp_to_local,
)
from instrument import resolve_instrument, yahoo_symbol


KLINE_CACHE_TTL = 60.0
KLINE_CACHE_MAX = 128
KLINE_FETCH_DAYS = 120
_CHART_WIDTH = 1000
_CHART_HEIGHT = 460
_MARGIN_LEFT = 20
_MARGIN_RIGHT = 86
_MARGIN_TOP = 36
_MARGIN_BOTTOM = 36
_UP_COLOR = "#d64541"
_DOWN_COLOR = "#1e9e6a"
_VISIBLE_BARS = 90


@dataclass
class DailyBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


@dataclass
class DailySeries:
    symbol: str
    market: str
    bars: list[DailyBar]
    name: str = ""


_daily_cache: dict[tuple[str, str], tuple[DailySeries, float]] = {}


def _provider_code(symbol: str, market: str) -> str:
    base, _ = resolve_instrument(symbol, market)
    if market == "HK":
        return f"hk{base.zfill(5)}"
    if market == "BSE":
        return f"bj{base.zfill(6)}"
    if market == "A":
        return f"{'sh' if base[0] in '569' else 'sz'}{base}"
    raise ValueError(f"no daily source for {symbol} ({market})")


def _normalize_bar(
    day: date,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float | None,
) -> DailyBar | None:
    values = (open_price, high, low, close)
    if any(not math.isfinite(value) or value <= 0 for value in values):
        return None
    return DailyBar(
        day=day,
        open=open_price,
        high=max(high, open_price, close),
        low=min(low, open_price, close),
        close=close,
        volume=volume if volume and volume > 0 else None,
    )


def _parse_tencent_rows(rows: list[list[str]]) -> list[DailyBar]:
    """Parse Tencent daily rows: ``date, open, close, high, low, volume``."""
    bars: list[DailyBar] = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            day = datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
            open_price = float(row[1])
            close = float(row[2])
            high = float(row[3])
            low = float(row[4])
            volume = float(row[5]) if row[5] else None
        except (TypeError, ValueError):
            continue
        bar = _normalize_bar(
            day, open_price, high, low, close, volume
        )
        if bar is not None:
            bars.append(bar)
    bars.sort(key=lambda item: item.day)
    return bars


def _parse_sina_rows(rows: list[dict]) -> list[DailyBar]:
    bars: list[DailyBar] = []
    for row in rows:
        try:
            day = datetime.strptime(
                str(row["day"])[:10], "%Y-%m-%d"
            ).date()
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            volume = float(row["volume"]) if row.get("volume") else None
        except (KeyError, TypeError, ValueError):
            continue
        bar = _normalize_bar(
            day, open_price, high, low, close, volume
        )
        if bar is not None:
            bars.append(bar)
    bars.sort(key=lambda item: item.day)
    return bars


async def _fetch_tencent(symbol: str, market: str) -> list[DailyBar]:
    code = _provider_code(symbol, market)
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={code},day,,,{KLINE_FETCH_DAYS},qfq"
    )
    client = get_client()
    resp = await client.get(
        url,
        headers={"Referer": "https://gu.qq.com/"},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()

    node = (payload.get("data") or {}).get(code) or {}
    rows = node.get("qfqday") or node.get("day") or []
    return _parse_tencent_rows(rows)


async def _fetch_sina(symbol: str, market: str) -> list[DailyBar]:
    code = _provider_code(symbol, market)
    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/"
        "CN_MarketDataService.getKLineData"
    )
    params = {
        "symbol": code,
        "scale": "240",
        "ma": "1",
        "datalen": str(KLINE_FETCH_DAYS),
    }
    client = get_client()
    resp = await client.get(
        url,
        params=params,
        headers={"Referer": "https://finance.sina.com.cn"},
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()

    if not isinstance(payload, list):
        return []
    return _parse_sina_rows(payload)


def _parse_yahoo_chart(result: dict) -> list[DailyBar]:
    timestamps = result.get("timestamp")
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or []
    if (
        not isinstance(timestamps, list)
        or not timestamps
        or not isinstance(quotes, list)
        or not quotes
        or not isinstance(quotes[0], dict)
    ):
        return []

    quote_node = quotes[0]
    opens = quote_node.get("open") or []
    highs = quote_node.get("high") or []
    lows = quote_node.get("low") or []
    closes = quote_node.get("close") or []
    volumes = quote_node.get("volume") or []
    tz = yahoo_exchange_tz(result.get("meta") or {})

    bars: list[DailyBar] = []
    for index, ts in enumerate(timestamps):
        if ts is None:
            continue
        try:
            open_price = float(opens[index])
            high = float(highs[index])
            low = float(lows[index])
            close = float(closes[index])
            raw_vol = volumes[index] if index < len(volumes) else None
            volume = float(raw_vol) if raw_vol is not None else 0.0
            day = yahoo_timestamp_to_local(ts, tz).date()
        except (IndexError, TypeError, ValueError, OSError):
            continue
        bar = _normalize_bar(
            day,
            open_price,
            high,
            low,
            close,
            volume if volume > 0 else None,
        )
        if bar is not None:
            bars.append(bar)
    bars.sort(key=lambda item: item.day)
    return bars


async def _fetch_yahoo(symbol: str, market: str) -> list[DailyBar]:
    result = await fetch_yahoo_chart(
        yahoo_symbol(symbol, market), range_="6mo", interval="1d"
    )
    if result is None:
        return []
    return _parse_yahoo_chart(result)


async def get_daily(quote) -> DailySeries | None:
    """Return recent daily bars for a quote, or ``None`` if unavailable."""
    if quote.market == "FUND":
        return None

    key = (quote.symbol, quote.market)
    now = time.time()
    cached = _daily_cache.get(key)
    if cached and now - cached[1] < KLINE_CACHE_TTL:
        return cached[0]

    try:
        if quote.market in ("US", "JP"):
            bars = await _fetch_yahoo(quote.symbol, quote.market)
        elif quote.market == "BSE":
            bars = await _fetch_sina(quote.symbol, quote.market)
        else:
            bars = await _fetch_tencent(quote.symbol, quote.market)
    except Exception:
        bars = []

    if len(bars) < 2:
        return None

    series = DailySeries(
        symbol=quote.symbol,
        market=quote.market,
        bars=bars[-KLINE_FETCH_DAYS:],
        name=getattr(quote, "name", "") or "",
    )
    cache_set(
        _daily_cache,
        key,
        series,
        now,
        ttl=KLINE_CACHE_TTL,
        max_size=KLINE_CACHE_MAX,
    )
    return series


def _decimals(price: float) -> int:
    return 3 if abs(price) < 10 else 2


def render_chart(
    series: DailySeries, prev_close: float | None = None
) -> bytes:
    """Render a daily candlestick chart as PNG bytes."""
    bars = series.bars[-_VISIBLE_BARS:]
    width, height = _CHART_WIDTH, _CHART_HEIGHT
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    x0 = _MARGIN_LEFT
    x1 = width - _MARGIN_RIGHT
    y0 = _MARGIN_TOP
    y1 = height - _MARGIN_BOTTOM
    plot_width = x1 - x0
    plot_height = y1 - y0
    price_bottom = y0 + plot_height * 0.76
    volume_top = price_bottom + 10

    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    hi = max(highs)
    lo = min(lows)
    if hi - lo < 1e-9:
        pad = max(abs(hi) * 0.002, 0.01)
    else:
        pad = (hi - lo) * 0.06
    lo -= pad
    hi += pad
    span = hi - lo

    count = len(bars)
    step = plot_width / count

    def price_y(value: float) -> float:
        return price_bottom - (value - lo) / span * (
            price_bottom - y0
        )

    small_font = load_font(13)
    title_font = load_font(18)

    for tick in range(5):
        value = lo + span * tick / 4
        y = price_y(value)
        draw.line((x0, y, x1, y), fill="#e3e6ea", width=1)
        draw.text(
            (x1 + 6, y - 7),
            f"{value:.{_decimals(value)}f}",
            fill="#4a4f55",
            font=small_font,
        )

    max_volume = max((bar.volume or 0.0 for bar in bars), default=0.0)
    body_width = max(1.0, min(step * 0.62, 14.0))
    for index, bar in enumerate(bars):
        center = x0 + step * (index + 0.5)
        color = _UP_COLOR if bar.close >= bar.open else _DOWN_COLOR

        draw.line(
            (center, price_y(bar.high), center, price_y(bar.low)),
            fill=color,
            width=1,
        )

        top = price_y(max(bar.open, bar.close))
        bottom = price_y(min(bar.open, bar.close))
        if bottom - top < 1:
            bottom = top + 1
        draw.rectangle(
            (center - body_width / 2, top, center + body_width / 2, bottom),
            fill=color,
        )

        if max_volume > 0 and bar.volume:
            bar_height = bar.volume / max_volume * (y1 - volume_top)
            light = "#f3c1c0" if bar.close >= bar.open else "#bfe3d4"
            draw.rectangle(
                (
                    center - body_width / 2,
                    y1 - bar_height,
                    center + body_width / 2,
                    y1,
                ),
                fill=light,
            )

    last = bars[-1]
    last_y = price_y(last.close)
    draw.line(
        (x0, last_y, x1, last_y), fill="#b0b6bc", width=1
    )
    draw.text(
        (x1 + 6, last_y - 7),
        f"{last.close:.{_decimals(last.close)}f}",
        fill="#222222",
        font=small_font,
    )

    tick_indices = (
        [round((count - 1) * tick / 4) for tick in range(5)]
        if count >= 5
        else list(range(count))
    )
    for index in tick_indices:
        label = bars[index].day.strftime("%m-%d")
        text_width = draw.textlength(label, font=small_font)
        text_x = min(
            max(x0 + step * (index + 0.5) - text_width / 2, x0), x1
        )
        draw.text(
            (text_x, y1 + 8), label, fill="#4a4f55", font=small_font
        )

    previous_close = (
        prev_close
        if prev_close and prev_close > 0
        else (bars[-2].close if count >= 2 else last.close)
    )
    change_pct = (
        (last.close - previous_close) / previous_close * 100
        if previous_close
        else 0.0
    )
    line_color = _UP_COLOR if change_pct >= 0 else _DOWN_COLOR
    title = (
        f"{series.name}({series.symbol})"
        if series.name and series.name != series.symbol and font_path()
        else series.symbol
    )
    summary = (
        f"{last.close:.{_decimals(last.close)}f}  {change_pct:+.2f}%"
    )
    summary_width = draw.textlength(summary, font=title_font)
    title = fit_text(
        draw, title, title_font, x1 - summary_width - x0 - 16
    )
    draw.text((x0, 12), title, fill="#222222", font=title_font)
    draw.text(
        (x1 - summary_width, 12),
        summary,
        fill=line_color,
        font=title_font,
    )

    draw.line((x0, price_bottom, x1, price_bottom), fill="#9aa3ad")
    draw.line((x0, y1, x1, y1), fill="#9aa3ad")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def clear_cache() -> None:
    _daily_cache.clear()
