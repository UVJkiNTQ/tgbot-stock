import glob
import os
import subprocess
from functools import lru_cache

from PIL import ImageFont


FONT_ENV_VAR = "BOT_CHART_FONT"

_CANDIDATES = (
    "~/.local/share/fonts/wqy-microhei.ttc",
    "~/.local/share/fonts/wqy-zenhei.ttc",
    "~/.local/share/fonts/NotoSansCJK-Regular.ttc",
    "~/.local/share/fonts/simkai.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)


def _fc_match() -> str | None:
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "sans-serif:lang=zh"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = result.stdout.strip()
    if path and os.path.exists(path):
        return path
    return None


@lru_cache(maxsize=1)
def font_path() -> str | None:
    """Locate a CJK-capable font file, if one is installed."""
    configured = os.getenv(FONT_ENV_VAR)
    if configured:
        path = os.path.expanduser(configured)
        if os.path.exists(path):
            return path

    for candidate in _CANDIDATES:
        path = os.path.expanduser(candidate)
        if os.path.exists(path):
            return path

    matched = _fc_match()
    if matched:
        return matched

    for pattern in (
        "~/.local/share/fonts/*.ttc",
        "~/.local/share/fonts/*.ttf",
        "~/.fonts/*.ttc",
        "~/.fonts/*.ttf",
    ):
        for path in sorted(glob.glob(os.path.expanduser(pattern))):
            return path
    return None


@lru_cache(maxsize=32)
def font(size: int) -> ImageFont.ImageFont:
    path = font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def fit_text(
    draw, text: str, font: ImageFont.ImageFont, max_width: float
) -> str:
    """Trim ``text`` with an ellipsis so it fits ``max_width`` pixels."""
    if max_width <= 0:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return f"{text}…" if text else ""
