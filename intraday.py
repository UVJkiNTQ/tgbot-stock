import asyncio
import io
import math
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
import yfinance as yf  # type: ignore
from PIL import Image, ImageDraw

from chartstyle import fit_text, font as load_font, font_path
from instrument import resolve_instrument


INTRADAY_CACHE_TTL = 20.0
_CHART_WIDTH = 1000
_CHART_HEIGHT = 460
_MARGIN_LEFT = 78
_MARGIN_RIGHT = 88
_MARGIN_TOP = 36
_MARGIN_BOTTOM = 36
_UP_COLOR = "#d64541"
_DOWN_COLOR = "#1e9e6a"
_FLAT_COLOR = "#555555"
_AVG_COLOR = "#f0a020"


@dataclass
class IntradayPoint:
    time: datetime
    price: float
    avg: float | None = None
    volume: float | None = None


@dataclass
class Intraday:
    symbol: str
    market: str
    prev_close: float
    points: list[IntradayPoint]
    name: str = ""


_intraday_cache: dict[tuple[str, str], tuple[Intraday, float]] = {}


def _tencent_code(symbol: str, market: str) -> str:
    base, _ = resolve_instrument(symbol, market)
    if market == "HK":
        return f"hk{base.zfill(5)}"
    if market == "BSE":
        return f"bj{base.zfill(6)}"
    if market == "A":
        return f"{'sh' if base[0] in '569' else 'sz'}{base}"
    raise ValueError(f"no minute source for {symbol} ({market})")


def _attach_vwap(points: list[IntradayPoint]) -> None:
    price_volume = 0.0
    volume_total = 0.0
    for point in points:
        if point.volume and point.volume > 0:
            price_volume += point.price * point.volume
            volume_total += point.volume
        point.avg = (
            price_volume / volume_total if volume_total > 0 else point.price
        )


def _parse_tencent_rows(
    rows: list[str], date_str: str
) -> list[IntradayPoint]:
    """Parse Tencent minute rows: ``HHMM price cumulative_volume [amount]``."""
    try:
        day = datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        return []

    points: list[IntradayPoint] = []
    cumulative_volume = 0.0
    for row in rows:
        parts = row.split()
        if len(parts) < 2:
            continue
        hhmm, price_text = parts[0], parts[1]
        if len(hhmm) != 4 or not hhmm.isdigit():
            continue
        try:
            price = float(price_text)
        except ValueError:
            continue
        if not math.isfinite(price) or price <= 0:
            continue

        volume: float | None = None
        if len(parts) >= 3:
            try:
                current = float(parts[2])
            except ValueError:
                current = None
            if current is not None and math.isfinite(current):
                if current >= cumulative_volume:
                    volume = current - cumulative_volume
                cumulative_volume = max(current, cumulative_volume)

        points.append(
            IntradayPoint(
                time=day.replace(
                    hour=int(hhmm[:2]), minute=int(hhmm[2:])
                ),
                price=price,
                volume=volume,
            )
        )

    _attach_vwap(points)
    return points


async def _fetch_tencent(symbol: str, market: str) -> list[IntradayPoint]:
    code = _tencent_code(symbol, market)
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
        f"?code={code}"
    )
    async with httpx.AsyncClient(
        timeout=8,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
        },
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()

    node = (payload.get("data") or {}).get(code) or {}
    body = node.get("data") or {}
    date_str = str(body.get("date") or "")
    if not date_str:
        return []
    return _parse_tencent_rows(body.get("data") or [], date_str)


def _fetch_us(symbol: str) -> list[IntradayPoint]:
    try:
        frame = yf.Ticker(symbol).history(
            period="1d",
            interval="1m",
            prepost=False,
            auto_adjust=False,
        )
    except Exception:
        return []
    if frame is None or frame.empty:
        return []

    points: list[IntradayPoint] = []
    for index, row in frame.iterrows():
        try:
            price = float(row["Close"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        try:
            volume = float(row.get("Volume") or 0.0)
        except (TypeError, ValueError):
            volume = 0.0
        moment = (
            index.to_pydatetime()
            if hasattr(index, "to_pydatetime")
            else index
        )
        points.append(
            IntradayPoint(
                time=moment,
                price=price,
                volume=volume if volume > 0 else None,
            )
        )

    _attach_vwap(points)
    return points


async def get_intraday(quote) -> Intraday | None:
    """Return same-day minute data for a quote, or ``None`` if unavailable."""
    if quote.market == "FUND":
        return None

    key = (quote.symbol, quote.market)
    now = time.time()
    cached = _intraday_cache.get(key)
    if cached and now - cached[1] < INTRADAY_CACHE_TTL:
        return cached[0]

    try:
        if quote.market == "US":
            points = await asyncio.to_thread(_fetch_us, quote.symbol)
        else:
            points = await _fetch_tencent(quote.symbol, quote.market)
    except Exception:
        points = []

    if len(points) < 2:
        return None

    prev_close = quote.prev_close
    if not math.isfinite(prev_close) or prev_close <= 0:
        prev_close = points[0].price

    data = Intraday(
        symbol=quote.symbol,
        market=quote.market,
        prev_close=prev_close,
        points=points,
        name=getattr(quote, "name", "") or "",
    )
    _intraday_cache[key] = (data, now)
    return data


def _dashed_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    fill: str,
    width: int = 1,
    dash: int = 6,
    gap: int = 4,
) -> None:
    x, y = start
    x2, _ = end
    while x < x2:
        draw.line((x, y, min(x + dash, x2), y), fill=fill, width=width)
        x += dash + gap


def _price_decimals(price: float) -> int:
    return 3 if abs(price) < 10 else 2


def render_chart(data: Intraday) -> bytes:
    """Render an intraday line chart as PNG bytes."""
    points = data.points
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

    prices = [point.price for point in points]
    averages = [point.avg for point in points if point.avg is not None]
    lo = min(prices + averages + [data.prev_close])
    hi = max(prices + averages + [data.prev_close])
    if hi - lo < 1e-9:
        pad = max(abs(hi) * 0.002, 0.01)
    else:
        pad = (hi - lo) * 0.06
    lo -= pad
    hi += pad
    span = hi - lo

    count = len(points)
    x_positions = [
        x0 + (plot_width * index / (count - 1)) for index in range(count)
    ]

    def price_y(value: float) -> float:
        return price_bottom - (value - lo) / span * (
            price_bottom - y0
        )

    small_font = load_font(13)
    title_font = load_font(18)

    grid_color = "#e3e6ea"
    axis_color = "#9aa3ad"
    for step in range(5):
        value = lo + span * step / 4
        y = price_y(value)
        draw.line((x0, y, x1, y), fill=grid_color, width=1)
        draw.text(
            (x1 + 6, y - 7),
            f"{value:.{_price_decimals(value)}f}",
            fill="#4a4f55",
            font=small_font,
        )
        if data.prev_close:
            pct = (value - data.prev_close) / data.prev_close * 100
            draw.text(
                (x0 - 58, y - 7),
                f"{pct:+.2f}%",
                fill="#8a9096",
                font=small_font,
            )

    baseline_y = price_y(data.prev_close)
    _dashed_line(draw, (x0, baseline_y), (x1, baseline_y), "#b0b6bc")
    draw.text(
        (x1 + 6, baseline_y - 7),
        f"{data.prev_close:.{_price_decimals(data.prev_close)}f}",
        fill="#8a9096",
        font=small_font,
    )

    last_price = prices[-1]
    if last_price > data.prev_close:
        line_color = _UP_COLOR
    elif last_price < data.prev_close:
        line_color = _DOWN_COLOR
    else:
        line_color = _FLAT_COLOR

    max_volume = max(
        (point.volume or 0.0 for point in points), default=0.0
    )
    if max_volume > 0:
        bar_width = max(1.0, plot_width / count * 0.6)
        for index, point in enumerate(points):
            if not point.volume:
                continue
            bar_height = point.volume / max_volume * (y1 - volume_top)
            previous = points[index - 1].price if index else point.price
            bar_color = (
                "#f3c1c0" if point.price >= previous else "#bfe3d4"
            )
            draw.rectangle(
                (
                    x_positions[index] - bar_width / 2,
                    y1 - bar_height,
                    x_positions[index] + bar_width / 2,
                    y1,
                ),
                fill=bar_color,
            )

    average_points = [
        (x_positions[index], price_y(point.avg))
        for index, point in enumerate(points)
        if point.avg is not None
    ]
    if len(average_points) >= 2:
        draw.line(average_points, fill=_AVG_COLOR, width=1)

    line_points = [
        (x_positions[index], price_y(point.price))
        for index, point in enumerate(points)
    ]
    fill_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    fill_draw = ImageDraw.Draw(fill_overlay)
    fill_draw.polygon(
        [(x0, price_bottom), *line_points, (x1, price_bottom)],
        fill=line_color + "24",
    )
    image = Image.alpha_composite(image.convert("RGBA"), fill_overlay).convert(
        "RGB"
    )
    draw = ImageDraw.Draw(image)
    draw.line(line_points, fill=line_color, width=2)

    marker_x, marker_y = line_points[-1]
    draw.ellipse(
        (marker_x - 3, marker_y - 3, marker_x + 3, marker_y + 3),
        fill=line_color,
    )

    for step in range(5):
        index = round((count - 1) * step / 4)
        label = points[index].time.strftime("%H:%M")
        text_width = draw.textlength(label, font=small_font)
        text_x = min(max(x_positions[index] - text_width / 2, x0), x1)
        draw.text(
            (text_x, y1 + 8), label, fill="#4a4f55", font=small_font
        )

    change = last_price - data.prev_close
    change_pct = (
        change / data.prev_close * 100 if data.prev_close else 0.0
    )
    title = (
        f"{data.name}({data.symbol})"
        if data.name and data.name != data.symbol and font_path()
        else data.symbol
    )
    summary = (
        f"{last_price:.{_price_decimals(last_price)}f}  "
        f"{change_pct:+.2f}%"
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

    draw.line((x0, price_bottom, x1, price_bottom), fill=axis_color)
    draw.line((x0, y1, x1, y1), fill=axis_color)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def clear_cache() -> None:
    _intraday_cache.clear()
