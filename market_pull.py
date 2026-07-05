"""
market_pull.py — Oklahoma Desk equity market data pull.

Daily closes for the 23 Active coverage names (see the vault's Coverage
Universe note), pulled from Yahoo Finance's chart endpoint, and written as:

  data/market/<TICKER>.csv     one year of (date, close) for trend charts
  data/market_latest.csv       one row per name: price + 1W/1M/YTD/1Y
                               returns + distance from the 52-week high
  data/market_narrative.txt    movers paragraph (Ollama or fallback)

Runs daily after the close, chained in run_edgar_watch.cmd.

Data-source honesty: Yahoo's v8 chart API is unofficial — free and stable
for years, but it can rate-limit or change without notice (the previous
candidate, stooq.com's CSV endpoint, 404'd everything when tested
2026-07-04). So this script degrades per-ticker: a failed symbol is
skipped with a warning and yesterday's CSV survives; it only aborts if
more than half the universe fails, which means the API itself broke.
"""

import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

import narrative

# The Active roster, mirroring 10 Equity Research/Coverage Universe.md.
# Sector order here is display order on the dashboard.
UNIVERSE = {
    "Energy - Upstream & Services": [
        ("DVN", "Devon Energy"), ("EXE", "Expand Energy"),
        ("HP", "Helmerich & Payne"), ("GPOR", "Gulfport Energy"),
        ("MNR", "Mach Natural Resources"), ("SD", "SandRidge Energy"),
        ("ARLP", "Alliance Resource"), ("TUSK", "Mammoth Energy"),
    ],
    "Energy - Midstream & Downstream": [
        ("OKE", "ONEOK"), ("WMB", "Williams Companies"),
        ("PSX", "Phillips 66"), ("NGL", "NGL Energy Partners"),
        ("DINO", "HF Sinclair"), ("CVI", "CVR Energy"),
        ("LXU", "LSB Industries"),
    ],
    "Financials & Utilities": [
        ("BOKF", "BOK Financial"), ("BANF", "BancFirst"),
        ("BSVN", "Bank7"), ("OGE", "OGE Energy"), ("OGS", "ONE Gas"),
    ],
    "Technology & Industrials": [
        ("PAYC", "Paycom Software"), ("AAON", "AAON"),
        ("MTRX", "Matrix Service"),
    ],
}

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "market"


def fetch_closes(sym: str) -> pd.DataFrame:
    """Two years of daily closes for one symbol (2y so YTD/1Y always fit)."""
    resp = requests.get(
        CHART_URL.format(sym=sym),
        params={"range": "2y", "interval": "1d"},
        headers=HEADERS, timeout=20,
    )
    resp.raise_for_status()
    result = resp.json()["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    df = pd.DataFrame({
        "date": [date.fromtimestamp(t) for t in stamps],
        "close": closes,
    }).dropna()
    return df.reset_index(drop=True)


def trailing_return(df: pd.DataFrame, sessions: int) -> float | None:
    """% return over the last N trading sessions."""
    if len(df) <= sessions:
        return None
    return round((df["close"].iloc[-1] / df["close"].iloc[-1 - sessions] - 1) * 100, 1)


def ytd_return(df: pd.DataFrame) -> float | None:
    """% return since the last close of the prior calendar year."""
    this_year = df["close"].iloc[-1:].index  # noqa: F841 — clarity below
    prior = df[df["date"].apply(lambda d: d.year) < df["date"].iloc[-1].year]
    if prior.empty:
        return None
    return round((df["close"].iloc[-1] / prior["close"].iloc[-1] - 1) * 100, 1)


def summarize(ticker: str, company: str, sector: str, df: pd.DataFrame) -> dict:
    year = df.tail(252)
    high_52w = year["close"].max()
    return {
        "ticker": ticker,
        "company": company,
        "sector": sector,
        "price": round(df["close"].iloc[-1], 2),
        "asof": df["date"].iloc[-1].isoformat(),
        "r1w": trailing_return(df, 5),
        "r1m": trailing_return(df, 21),
        "rytd": ytd_return(df),
        "r1y": trailing_return(df, 252),
        "off_52w_high_pct": round((df["close"].iloc[-1] / high_52w - 1) * 100, 1),
    }


def rule_based_summary(rows: list[dict]) -> str:
    ranked = sorted([r for r in rows if r["r1w"] is not None], key=lambda r: r["r1w"])
    return (f"Week's best: {ranked[-1]['ticker']} {ranked[-1]['r1w']:+.1f}%; "
            f"worst: {ranked[0]['ticker']} {ranked[0]['r1w']:+.1f}%.")


def generate_narrative(rows: list[dict]) -> str:
    """Movers paragraph — same contract as everywhere else: Python computes
    the superlatives, the model only phrases them."""
    by_1w = sorted([r for r in rows if r["r1w"] is not None], key=lambda r: r["r1w"])
    by_ytd = sorted([r for r in rows if r["rytd"] is not None], key=lambda r: r["rytd"])
    facts = [
        f"- Best week: {r['ticker']} ({r['company']}) {r['r1w']:+.1f}%"
        for r in by_1w[-3:][::-1]
    ] + [
        f"- Worst week: {r['ticker']} ({r['company']}) {r['r1w']:+.1f}%"
        for r in by_1w[:3]
    ] + [
        f"- YTD leader: {by_ytd[-1]['ticker']} {by_ytd[-1]['rytd']:+.1f}%; "
        f"YTD laggard: {by_ytd[0]['ticker']} {by_ytd[0]['rytd']:+.1f}%",
    ]
    prompt = f"""You are writing a 2-3 sentence market wrap for a research desk
covering Oklahoma-connected stocks. Below are pre-computed facts — do not
compute, rank, or add any number or ticker not listed. Phrase them into
plain prose, no bullet points, no preamble, no disclaimers.

FACTS:
{chr(10).join(facts)}"""
    return narrative.generate(prompt) or rule_based_summary(rows)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows, failures = [], []

    for sector, names in UNIVERSE.items():
        for ticker, company in names:
            time.sleep(0.4)  # unofficial API — stay polite
            try:
                df = fetch_closes(ticker)
                if len(df) < 30:
                    raise ValueError(f"only {len(df)} closes returned")
            except Exception as exc:  # noqa: BLE001 — skip-and-warn by design
                failures.append(ticker)
                print(f"{ticker}: fetch failed ({exc}) -- kept yesterday's data")
                continue
            df.tail(252).to_csv(DATA_DIR / f"{ticker}.csv", index=False)
            rows.append(summarize(ticker, company, sector, df))
            print(f"{ticker:<5} {rows[-1]['price']:>9,.2f}  1w {rows[-1]['r1w']:+.1f}%")

    total = sum(len(v) for v in UNIVERSE.values())
    if len(failures) > total / 2:
        raise SystemExit(
            f"{len(failures)}/{total} tickers failed — Yahoo endpoint likely "
            "broke; nothing written to market_latest.csv"
        )

    out = pd.DataFrame(rows)
    out_path = BASE_DIR / "data" / "market_latest.csv"
    out.to_csv(out_path, index=False)
    print(f"\n{len(rows)}/{total} names -> {out_path}")
    if failures:
        print(f"skipped: {', '.join(failures)}")

    text = generate_narrative(rows)
    (BASE_DIR / "data" / "market_narrative.txt").write_text(text, encoding="utf-8")
    print("movers narrative written")


if __name__ == "__main__":
    main()
