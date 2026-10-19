"""Scan universes. NSE index membership changes; pass ZBOT_UNIVERSE=SYM1,SYM2 to override."""

# M&M is left out: the agent rejects "&" in tickers (path-safety check).
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BPCL", "BHARTIARTL", "BRITANNIA", "CIPLA", "COALINDIA",
    "DIVISLAB", "DRREDDY", "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC", "INDUSINDBK", "INFY",
    "JSWSTEEL", "KOTAKBANK", "LT", "MARUTI", "NTPC", "NESTLEIND", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SUNPHARMA", "TCS", "TATACONSUM",
    "TATAMOTORS", "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO", "SHRIRAMFIN",
]


def resolve(spec: str) -> list[str]:
    if spec.strip().upper() == "NIFTY50":
        return list(NIFTY50)
    return [s.strip().upper() for s in spec.split(",") if s.strip()]
