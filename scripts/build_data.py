#!/usr/bin/env python3
"""
Fetch daily OHLC for all theme ETFs and compute the Themes-tab metrics.
Runs headless in GitHub Actions after US close. Output: docs/snapshot.json

Metric definitions (replicating the Excel tab, bugs fixed):
  daily        = close / prev_close - 1
  roll_w       = close / close[5 sessions ago] - 1
  roll_m       = close / close[21 sessions ago] - 1
  ytd          = close / last close of prior year - 1
  w1 / m1 / y1 = calendar 7/30/365-day lookups (nearest prior session)
  off52h       = close / max(close, 252 sessions) - 1        # FIXED sign convention
  vs10/21/50   = close / SMA(n) - 1
  g6_50        = SMA6 > SMA50 ; g21_50 = SMA21 > SMA50
  rs_line      = close / SPY_close
  rs_sts       = percentile rank (inclusive) of today's RS value within
                 trailing 63 sessions of RS values                # FIXED: no #NUM! on short history (needs >=21 obs, else null)
  intraday     = close / open - 1
"""
import json, sys, time, datetime as dt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
UNIVERSE = json.loads((ROOT / "scripts" / "universe.json").read_text())
OUT = ROOT / "docs" / "snapshot.json"

SYM = {u["ticker"]: u.get("yahoo", u["ticker"]) for u in UNIVERSE}
TICKERS = sorted(set(SYM.values()) | {"SPY"})


def fetch_history() -> pd.DataFrame:
    """Adjusted daily closes + opens, ~420 sessions. yfinance primary, stooq fallback."""
    import yfinance as yf
    df = yf.download(TICKERS, period="2y", interval="1d",
                     auto_adjust=True, progress=False, group_by="ticker", threads=True)
    return df


def stooq_fallback(ticker: str) -> pd.DataFrame | None:
    try:
        from pandas_datareader import data as pdr
        d = pdr.DataReader(f"{ticker}.US", "stooq").sort_index()
        return d
    except Exception:
        return None


def best_hk_counter(code_hk: str, hist, tickers_avail) -> str:
    """For HK 9xxx USD counters, prefer whichever counter (9xxx USD vs 2xxx/3xxx HKD twin)
    has the larger 63-day median dollar volume. Returns the yahoo symbol to use."""
    base = code_hk.split(".")[0]
    if not base.startswith("9") or len(base) != 4:
        return code_hk
    candidates = [code_hk] + [p + base[1:] + ".HK" for p in ("2", "3")]
    best, best_dv = code_hk, -1.0
    import yfinance as yf
    for cand in candidates:
        try:
            d = yf.download(cand, period="6mo", interval="1d", auto_adjust=True, progress=False)
            if d.empty:
                continue
            dv = float((d["Close"].squeeze() * d["Volume"].squeeze()).tail(63).median())
            if dv > best_dv:
                best, best_dv = cand, dv
        except Exception:
            continue
    return best


def metrics_for(close: pd.Series, open_: pd.Series, spy: pd.Series) -> dict:
    close = close.dropna()
    if len(close) < 60:
        return {}
    c = close.iloc[-1]
    m = {}
    m["price"] = round(float(c), 4)
    m["intraday"] = float(c / open_.dropna().iloc[-1] - 1) if len(open_.dropna()) else None
    m["daily"] = float(c / close.iloc[-2] - 1)
    m["roll_w"] = float(c / close.iloc[-6] - 1)
    m["roll_m"] = float(c / close.iloc[-22] - 1) if len(close) >= 22 else None
    # YTD: last close strictly before Jan 1 of current year
    year = close.index[-1].year
    prior = close[close.index < pd.Timestamp(year, 1, 1)]
    m["ytd"] = float(c / prior.iloc[-1] - 1) if len(prior) else None
    # calendar lookbacks
    for key, days in (("w1", 7), ("m1", 30), ("y1", 365)):
        cutoff = close.index[-1] - pd.Timedelta(days=days)
        ref = close[close.index <= cutoff]
        m[key] = float(c / ref.iloc[-1] - 1) if len(ref) else None
    # 52wk high (fixed sign: at high = 0, below high = negative)
    hi = close.tail(252).max()
    m["off52h"] = float(c / hi - 1)
    # SMAs
    for n in (6, 10, 21, 50):
        m[f"sma{n}"] = float(close.tail(n).mean()) if len(close) >= n else None
    m["vs10"] = c / m["sma10"] - 1
    m["vs21"] = c / m["sma21"] - 1
    m["vs50"] = c / m["sma50"] - 1
    m["g6_50"] = "YES" if m["sma6"] > m["sma50"] else "NO"
    m["g21_50"] = "YES" if m["sma21"] > m["sma50"] else "NO"
    # RS vs SPY + percentile rank over trailing 63 sessions (inclusive -> no #NUM!)
    rs = (close / spy.reindex(close.index)).dropna().tail(63)
    m["rs_sts"] = float((rs <= rs.iloc[-1]).mean()) if len(rs) >= 21 else None
    # 1-Month RS: excess rolling-monthly return vs SPY
    if len(close) >= 22 and len(spy) >= 22:
        m["rs_1m"] = float((c / close.iloc[-22]) / (spy.iloc[-1] / spy.iloc[-22]) - 1)
    else:
        m["rs_1m"] = None
    return m


def main():
    hist = fetch_history()
    spy = hist["SPY"]["Close"].dropna()
    rows = []
    for u in UNIVERSE:
        t = u["ticker"]
        y = SYM[t]
        if y.endswith(".HK") and y.split(".")[0].startswith("9"):
            y2 = best_hk_counter(y, hist, TICKERS)
            if y2 != y:
                u = {**u, "counter_used": y2}
                y = y2
                # fetch chosen counter solo since batch didn't include it
                try:
                    import yfinance as yf
                    solo = yf.download(y, period="2y", interval="1d", auto_adjust=True, progress=False)
                    if not solo.empty:
                        hist = hist  # unchanged; use solo directly below
                        close, open_ = solo["Close"].squeeze(), solo["Open"].squeeze()
                        m = metrics_for(close, open_, spy)
                        rows.append({**u, **m}); continue
                except Exception:
                    pass
        try:
            close, open_ = hist[y]["Close"], hist[y]["Open"]
        except KeyError:
            close = open_ = pd.Series(dtype=float)
        if close.dropna().empty:
            try:
                import yfinance as yf
                solo = yf.download(y, period="2y", interval="1d",
                                   auto_adjust=True, progress=False)
                if not solo.empty:
                    close = solo["Close"].squeeze(); open_ = solo["Open"].squeeze()
            except Exception:
                pass
        if close.dropna().empty and y.startswith("ETPM"):
            for alt in ("80019.AX", "80022.AX", "80023.AX", "80018.AX"):
                try:
                    import yfinance as yf
                    solo = yf.download(alt, period="2y", interval="1d", auto_adjust=True, progress=False)
                    if not solo.empty:
                        close = solo["Close"].squeeze(); open_ = solo["Open"].squeeze()
                        break
                except Exception:
                    continue
        if close.dropna().empty:
            sq = stooq_fallback(y)
            if sq is not None and not sq.empty:
                close, open_ = sq["Close"], sq["Open"]
        m = metrics_for(close, open_, spy)
        # Live AUM + expense ratio for ETF sections (rows carrying an mcap field)
        if "mcap" in u and u.get("group") != "Market Benchmarks":
            try:
                import yfinance as yf
                info = yf.Ticker(y).info or {}
                ta = info.get("totalAssets")
                if ta:
                    m["mcap"] = float(ta)
                er = info.get("netExpenseRatio") or info.get("annualReportExpenseRatio") or info.get("expenseRatio")
                if er:
                    # Yahoo returns some ratios as percent (0.40) and some as fraction (0.004); normalize to fraction
                    m["exp"] = float(er)/100 if er > 0.5 else float(er)
            except Exception:
                pass  # keep static values from universe.json
        # Live TTM yield for income ETFs (universe entries carrying a 'yield' field)
        if u.get("yield") is not None and m.get("price"):
            try:
                import yfinance as yf
                divs = yf.Ticker(y).dividends
                if divs is not None and len(divs):
                    divs.index = divs.index.tz_localize(None)
                    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=365)
                    ttm = float(divs[divs.index >= cutoff].sum())
                    if ttm > 0:
                        m["yield"] = round(ttm / m["price"], 5)
            except Exception:
                pass  # keep last known yield from universe.json
        rows.append({**u, **m})
        # keys: group, theme, ticker, long, short + metrics

    as_of = str(spy.index[-1].date())
    OUT.write_text(json.dumps({"as_of": as_of,
                               "generated_utc": dt.datetime.utcnow().isoformat(timespec="seconds"),
                               "rows": rows}, indent=1))
    missing = [r["ticker"] for r in rows if "price" not in r]
    print(f"wrote {len(rows)} rows, as_of {as_of}; missing data: {missing or 'none'}")
    if len(missing) > len(rows) * 0.3:
        sys.exit(1)  # fail the workflow loudly rather than publish a mostly-empty board


if __name__ == "__main__":
    main()
