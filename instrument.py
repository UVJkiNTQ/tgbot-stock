MARKETS = frozenset({"A", "HK", "US", "FUND"})

_MARKET_SUFFIX = {
    "A": ".A",
    "HK": ".HK",
    "US": ".US",
    "FUND": ".F",
}


def normalize_symbol(symbol: str) -> str:
    """Return the canonical provider/database symbol without a market suffix."""
    s = symbol.strip().upper()
    if not s:
        raise ValueError("symbol cannot be empty")

    if s.endswith(".F"):
        s = s[:-2]
    elif s.endswith(".A"):
        s = s[:-2]
    elif s.endswith(".US"):
        s = s[:-3]
    elif s.endswith(".HK") and s[:-3].isdigit():
        s = s[:-3]
    elif s.startswith("HK") and s[2:].isdigit():
        s = s[2:]

    if s.isdigit() and len(s) <= 5:
        return s.zfill(5)
    return s


def parse_symbol(symbol: str) -> tuple[str, str | None]:
    """Return the canonical symbol and any explicitly requested market."""
    s = symbol.strip().upper()
    if not s:
        raise ValueError("symbol cannot be empty")

    forced: str | None = None
    if s.endswith(".F"):
        forced = "FUND"
        s = s[:-2]
    elif s.endswith(".A"):
        forced = "A"
        s = s[:-2]
    elif s.endswith(".US"):
        forced = "US"
        s = s[:-3]
    elif s.endswith(".HK") and s[:-3].isdigit():
        forced = "HK"
        s = s[:-3]

    if forced == "FUND":
        return s, forced
    return normalize_symbol(s), forced


def detect_market(symbol: str) -> str:
    s = normalize_symbol(symbol)
    if s.isdigit():
        if len(s) == 5:
            return "HK"
        if len(s) == 6:
            return "A"
    return "US"


def normalize_market(market: str) -> str:
    normalized = market.strip().upper()
    if normalized not in MARKETS:
        raise ValueError(f"unsupported market: {market}")
    return normalized


def resolve_instrument(
    symbol: str, market: str | None = None
) -> tuple[str, str]:
    """Return the stable (symbol, market) identity used in cache and storage."""
    base, forced = parse_symbol(symbol)
    explicit = normalize_market(market) if market is not None else None
    if forced is not None and explicit is not None and forced != explicit:
        raise ValueError(
            f"symbol qualifier {forced} conflicts with market {explicit}"
        )
    return base, explicit or forced or detect_market(base)


def qualified_symbol(symbol: str, market: str) -> str:
    base, resolved_market = resolve_instrument(symbol, market)
    return f"{base}{_MARKET_SUFFIX[resolved_market]}"
