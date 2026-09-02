MARKETS = frozenset({"A", "HK", "US", "FUND", "BSE"})

_MARKET_SUFFIX = {
    "A": ".A",
    "HK": ".HK",
    "US": ".US",
    "FUND": ".F",
    "BSE": ".BSE",
}

# These are code *segments*, rather than a claim that every code currently in
# a segment is listed.  They are kept here as the single source of truth for
# symbol classification used by quotes, trades, and database migration.
#
# 000/001/002/003 are intentionally not in the fund-only set: those segments
# are shared by A-shares and fund products.  004-019 are fund-only under the
# code convention used by this bot; in particular 010042 is an OTC fund and
# cannot be an A-share code.  A caller that has a shared code must use .A/.F.
A_SHARE_PREFIXES = frozenset(
    {
        "000", "001", "002", "003", "200", "300", "301",
        "600", "601", "603", "605", "688", "689", "900",
        # Exchange-traded funds supported by the Sina quote path.
        "150", "159", "500", "501", "502", "510", "511", "512",
        "513", "515", "516", "517", "518", "519",
    }
)
BSE_PREFIXES = frozenset(
    {
        "430",
        *(f"{prefix:03d}" for prefix in range(830, 840)),
        *(f"{prefix:03d}" for prefix in range(870, 880)),
        "920",
    }
)
OTC_FUND_PREFIXES = frozenset(
    f"{prefix:03d}" for prefix in range(4, 20)
)
AMBIGUOUS_PREFIXES = frozenset({"000", "001", "002", "003"})


def normalize_symbol(symbol: str) -> str:
    """Return the canonical provider/database symbol without a market suffix."""
    s = symbol.strip().upper()
    if not s:
        raise ValueError("symbol cannot be empty")

    if s.endswith(".BSE"):
        s = s[:-4]
    elif s.endswith(".BJ"):
        s = s[:-3]
    elif s.endswith(".F"):
        s = s[:-2]
    elif s.endswith(".A"):
        s = s[:-2]
    elif s.endswith(".US"):
        s = s[:-3]
    elif s.endswith(".HK") and s[:-3].isdigit():
        s = s[:-3]
    elif s.startswith("HK") and s[2:].isdigit():
        s = s[2:]
    elif s.startswith("BJ") and s[2:].isdigit():
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
    if s.endswith(".BSE") or s.endswith(".BJ"):
        forced = "BSE"
        suffix_length = 4 if s.endswith(".BSE") else 3
        s = s[:-suffix_length]
    elif s.endswith(".F"):
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
    elif s.startswith("BJ") and s[2:].isdigit():
        forced = "BSE"
        s = s[2:]

    if forced == "FUND":
        return s, forced
    return normalize_symbol(s), forced


def market_candidates(symbol: str) -> frozenset[str]:
    """Return markets compatible with a symbol's code pattern.

    A six-digit code can be shared by an A-share and a fund.  This function
    exposes that fact to callers that need to validate or present a choice;
    ``detect_market`` keeps the historical A-share fallback for compatibility
    when no explicit qualifier was supplied.
    """
    s = normalize_symbol(symbol)
    if not s.isdigit():
        return frozenset({"US"})
    if len(s) == 5:
        return frozenset({"HK"})
    if len(s) != 6:
        return frozenset()

    prefix = s[:3]
    if prefix in BSE_PREFIXES:
        return frozenset({"BSE"})
    if prefix in OTC_FUND_PREFIXES:
        return frozenset({"FUND"})
    if prefix in AMBIGUOUS_PREFIXES:
        return frozenset({"A", "FUND"})
    if prefix in A_SHARE_PREFIXES:
        return frozenset({"A"})
    # The fund catalogue is not a disjoint prefix namespace.  An unknown
    # six-digit code therefore remains unresolved rather than being claimed
    # to be one specific market.
    return frozenset({"A", "FUND"})


def detect_market(symbol: str) -> str:
    s = normalize_symbol(symbol)
    candidates = market_candidates(s)
    if candidates == frozenset({"HK"}):
        return "HK"
    if candidates == frozenset({"BSE"}):
        return "BSE"
    if candidates == frozenset({"FUND"}):
        return "FUND"
    if "A" in candidates:
        return "A"
    return "US"


def normalize_market(market: str) -> str:
    normalized = market.strip().upper()
    if normalized == "BJ":
        normalized = "BSE"
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
