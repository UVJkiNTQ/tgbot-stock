use std::collections::BTreeSet;

pub const MARKETS: &[&str] = &["A", "BSE", "FUND", "HK", "JP", "US"];

pub fn is_valid_market(market: &str) -> bool {
    MARKETS.contains(&market)
}

pub fn market_suffix(market: &str) -> &'static str {
    match market {
        "A" => ".A",
        "HK" => ".HK",
        "US" => ".US",
        "FUND" => ".F",
        "BSE" => ".BSE",
        "JP" => ".JP",
        _ => "",
    }
}

fn is_a_share_prefix(p: &str) -> bool {
    matches!(
        p,
        "000"
            | "001"
            | "002"
            | "003"
            | "200"
            | "300"
            | "301"
            | "600"
            | "601"
            | "603"
            | "605"
            | "688"
            | "689"
            | "900"
            | "150"
            | "159"
            | "500"
            | "501"
            | "502"
            | "510"
            | "511"
            | "512"
            | "513"
            | "515"
            | "516"
            | "517"
            | "518"
            | "519"
    )
}

fn is_bse_prefix(p: &str) -> bool {
    if p == "430" || p == "920" {
        return true;
    }
    if let Ok(n) = p.parse::<u32>() {
        return (830..840).contains(&n) || (870..880).contains(&n);
    }
    false
}

fn is_otc_fund_prefix(p: &str) -> bool {
    if p.len() == 3 && p.bytes().all(|b| b.is_ascii_digit()) {
        if let Ok(n) = p.parse::<u32>() {
            return (4..20).contains(&n);
        }
    }
    false
}

fn is_ambiguous_prefix(p: &str) -> bool {
    matches!(p, "000" | "001" | "002" | "003")
}

fn is_all_digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

pub fn zfill(s: &str, width: usize) -> String {
    if s.len() >= width {
        s.to_string()
    } else {
        format!("{}{}", "0".repeat(width - s.len()), s)
    }
}

pub fn normalize_symbol(symbol: &str) -> Result<String, String> {
    let mut s = symbol.trim().to_uppercase();
    if s.is_empty() {
        return Err("symbol cannot be empty".to_string());
    }

    let mut japan = false;
    if let Some(rest) = s.strip_suffix(".JP") {
        s = rest.to_string();
        japan = true;
    } else if s.starts_with("JP") && is_all_digits(&s[2..]) {
        s = s[2..].to_string();
        japan = true;
    } else if let Some(rest) = s.strip_suffix(".BSE") {
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".BJ") {
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".F") {
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".A") {
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".US") {
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".HK") {
        if is_all_digits(rest) {
            s = rest.to_string();
        }
    } else if s.starts_with("HK") && is_all_digits(&s[2..]) {
        s = s[2..].to_string();
    } else if s.starts_with("BJ") && is_all_digits(&s[2..]) {
        s = s[2..].to_string();
    }

    if is_all_digits(&s) && s.len() <= 5 {
        return Ok(if japan { s } else { zfill(&s, 5) });
    }
    Ok(s)
}

pub fn parse_symbol(symbol: &str) -> Result<(String, Option<String>), String> {
    let mut s = symbol.trim().to_uppercase();
    if s.is_empty() {
        return Err("symbol cannot be empty".to_string());
    }

    let mut forced: Option<&'static str> = None;
    if let Some(rest) = s.strip_suffix(".JP") {
        forced = Some("JP");
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".BSE") {
        forced = Some("BSE");
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".BJ") {
        forced = Some("BSE");
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".F") {
        forced = Some("FUND");
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".A") {
        forced = Some("A");
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".US") {
        forced = Some("US");
        s = rest.to_string();
    } else if let Some(rest) = s.strip_suffix(".HK") {
        if is_all_digits(rest) {
            forced = Some("HK");
            s = rest.to_string();
        }
    } else if s.starts_with("JP") && is_all_digits(&s[2..]) {
        forced = Some("JP");
        s = s[2..].to_string();
    } else if s.starts_with("BJ") && is_all_digits(&s[2..]) {
        forced = Some("BSE");
        s = s[2..].to_string();
    }

    if matches!(forced, Some("FUND") | Some("JP")) {
        return Ok((s, forced.map(str::to_string)));
    }
    Ok((normalize_symbol(&s)?, forced.map(str::to_string)))
}

pub fn market_candidates(symbol: &str) -> Result<BTreeSet<String>, String> {
    let (base, forced) = parse_symbol(symbol)?;
    if let Some(m) = forced {
        return Ok(BTreeSet::from([m]));
    }
    if !is_all_digits(&base) {
        return Ok(BTreeSet::from(["US".to_string()]));
    }
    if base.len() == 5 {
        return Ok(BTreeSet::from(["HK".to_string()]));
    }
    if base.len() != 6 {
        return Ok(BTreeSet::new());
    }

    let prefix = &base[..3];
    if is_bse_prefix(prefix) {
        return Ok(BTreeSet::from(["BSE".to_string()]));
    }
    if is_otc_fund_prefix(prefix) {
        return Ok(BTreeSet::from(["FUND".to_string()]));
    }
    if is_ambiguous_prefix(prefix) {
        return Ok(BTreeSet::from(["A".to_string(), "FUND".to_string()]));
    }
    if is_a_share_prefix(prefix) {
        return Ok(BTreeSet::from(["A".to_string()]));
    }
    Ok(BTreeSet::from(["A".to_string(), "FUND".to_string()]))
}

pub fn detect_market(symbol: &str) -> Result<String, String> {
    let (_, forced) = parse_symbol(symbol)?;
    if let Some(m) = forced {
        return Ok(m);
    }
    let s = normalize_symbol(symbol)?;
    let candidates = market_candidates(&s)?;
    if candidates.len() == 1 {
        if let Some(only) = candidates.iter().next() {
            if matches!(only.as_str(), "HK" | "BSE" | "FUND") {
                return Ok(only.clone());
            }
        }
    }
    if candidates.contains("A") {
        return Ok("A".to_string());
    }
    Ok("US".to_string())
}

pub fn normalize_market(market: &str) -> Result<String, String> {
    let mut normalized = market.trim().to_uppercase();
    if normalized == "BJ" {
        normalized = "BSE".to_string();
    }
    if !is_valid_market(&normalized) {
        return Err(format!("unsupported market: {market}"));
    }
    Ok(normalized)
}

pub fn resolve_instrument(symbol: &str, market: Option<&str>) -> Result<(String, String), String> {
    let (mut base, forced) = parse_symbol(symbol)?;
    let explicit = match market {
        Some(m) => Some(normalize_market(m)?),
        None => None,
    };
    if let (Some(f), Some(e)) = (&forced, &explicit) {
        if f != e {
            return Err(format!("symbol qualifier {f} conflicts with market {e}"));
        }
    }
    let chosen = explicit.or(forced);
    if chosen.as_deref() == Some("JP")
        && base.len() == 5
        && base.starts_with('0')
        && is_all_digits(&base)
    {
        base = base[1..].to_string();
    }
    let resolved_market = match chosen {
        Some(m) => m,
        None => detect_market(&base)?,
    };
    Ok((base, resolved_market))
}

pub fn qualified_symbol(symbol: &str, market: &str) -> Result<String, String> {
    let (base, resolved_market) = resolve_instrument(symbol, Some(market))?;
    Ok(format!("{base}{}", market_suffix(&resolved_market)))
}

pub fn yahoo_symbol(symbol: &str, market: &str) -> Result<String, String> {
    let (base, resolved_market) = resolve_instrument(symbol, Some(market))?;
    if resolved_market == "JP" {
        return Ok(format!("{base}.T"));
    }
    Ok(base)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hong_kong_aliases() {
        for raw in ["700", "0700", "00700", "0700.HK", "HK00700"] {
            assert_eq!(normalize_symbol(raw).unwrap(), "00700");
            assert_eq!(detect_market(raw).unwrap(), "HK");
        }
    }

    #[test]
    fn otc_fund_and_ambiguous_codes() {
        assert_eq!(detect_market("010042").unwrap(), "FUND");
        assert_eq!(
            market_candidates("002714").unwrap(),
            BTreeSet::from(["A".to_string(), "FUND".to_string()])
        );
        assert_eq!(detect_market("002714").unwrap(), "A");
    }

    #[test]
    fn bse_and_japan_codes() {
        for raw in ["430047", "830799", "871970", "920118", "BJ920118", "920118.BJ"] {
            assert_eq!(detect_market(raw).unwrap(), "BSE");
        }
        for raw in ["7203.JP", "jp7203", "JP7203"] {
            assert_eq!(normalize_symbol(raw).unwrap(), "7203");
            assert_eq!(detect_market(raw).unwrap(), "JP");
            assert_eq!(yahoo_symbol(raw, "JP").unwrap(), "7203.T");
        }
        assert_eq!(normalize_symbol("7203").unwrap(), "07203");
        assert_eq!(detect_market("7203").unwrap(), "HK");
    }

    #[test]
    fn us_ticker_hkd() {
        assert_eq!(normalize_symbol("HKD").unwrap(), "HKD");
        assert_eq!(detect_market("HKD").unwrap(), "US");
    }
}
