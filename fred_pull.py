"""
fred_pull.py — Oklahoma Desk Econ Monitor data pull.

Pulls the FRED series defined in the vault note "Econ Monitor Methodology",
computes two synthetic OK-vs-US series and a 6-month ARIMA projection, and
writes:

  data/fred/<SERIES_ID>.csv    raw history from START_DATE, one per series
  data/fred/UR_GAP.csv         synthetic: OK minus US unemployment (ppt)
  data/fred/PAYROLL_DIFF.csv   synthetic: OK minus US payroll growth (ppt y/y)
  data/fred/OKUR_forecast.csv  6-month ARIMA(1,1,1) projection of OKUR
  data/fred_latest.csv         one row per visible series -> dashboard cards
  data/fred_flags.json         deterministic regime checks -> dashboard pills
  data/fred_narrative.txt      "what moved" paragraph (Ollama or fallback)

Setup:
  1. Get a free API key: https://fredaccount.stlouisfed.org/apikeys
  2. Put it in the environment (or a .env file next to this script):
       FRED_API_KEY=abc123...
  3. python fred_pull.py

Why the official API and not the website: the keyless fredgraph.csv endpoint
is undocumented and has intermittently 403-blocked automated clients, while
api.stlouisfed.org is the documented, supported path for unattended pulls.
"""

import csv
import json
import math
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

import narrative

# Series IDs verified against fred.stlouisfed.org on 2026-07-03/04. Two
# hard-won naming lessons: FRED does NOT host the raw BLS "SMS..." pattern
# for state/metro payroll levels (use legacy ids: TULS140NA, OKLA440NA,
# OKNRMN), and the Philly Fed state LEADING index (OKSLIND) is discontinued
# — dead since Feb 2020, never resuming — so the coincident index is the
# only Philly Fed state read still alive.
#
# Per-series options:
#   group               dashboard section this card belongs to
#   tickers             coverage-universe names this series feeds (static,
#                       the desk's own stated linkage — see Coverage Universe)
#   higher_is_tailwind  is a RISE good news for those tickers? Judged here
#                       once, never re-derived by the narrative model (an
#                       early draft let the model infer direction and it
#                       called rising unemployment a "tailwind for banks")
#   is_rate             already a % (or ppt) — y/y shows as a POINT change,
#                       never percent-of-a-percent ("+32%" on a 3.1->4.1
#                       unemployment move is true and useless)
#   transform           smoothing applied before the card number (raw data
#                       still lands in the CSV): ma4 = 4-period moving avg
#   context             no direction call — rates cut differently
#                       across banks/utilities/munis, so the card informs
#                       without pretending one direction is "good"
#   hidden              fetched (feeds a synthetic series) but no card
SERIES = {
    # ---------------------------------------------------- Labor Market
    "OKUR": {
        "description": "OK unemployment rate (%, SA, monthly)",
        "blurb": "Share of the state labor force out of work — the headline gauge of Oklahoma economic health",
        "group": "Labor Market",
        "tickers": ["BOKF", "BANF", "BSVN", "PAYC"],
        "higher_is_tailwind": False,
        "is_rate": True,
    },
    "OKNA": {
        "description": "OK nonfarm payrolls (thousands, SA, monthly)",
        "blurb": "Total jobs in the state — the broadest read on whether Oklahoma is adding or losing work",
        "group": "Labor Market",
        "tickers": ["BOKF", "BANF", "PAYC", "OGE"],
        "higher_is_tailwind": True,
        "is_rate": False,
    },
    "OKICLAIMS": {
        "description": "OK initial jobless claims, 4-wk avg (weekly, NSA)",
        "blurb": "New unemployment filings each week — the fastest-moving distress signal; rises here reach the jobless rate months later",
        "group": "Labor Market",
        "tickers": ["BOKF", "BANF", "BSVN", "PAYC"],
        "higher_is_tailwind": False,   # more claims = more labor stress
        "is_rate": False,
        "transform": "ma4",  # single weeks are noisy + holiday-distorted (NSA)
    },
    "OKPHCI": {
        "description": "OK coincident index (2007=100, SA, monthly)",
        "blurb": "Philly Fed composite of jobs, hours, and wages — one number for the state business cycle",
        "group": "Labor Market",
        "tickers": ["BOKF", "BANF", "BSVN"],
        "higher_is_tailwind": True,
        "is_rate": False,
    },
    # ------------------------------------------------ Oklahoma vs Nation
    # (synthetic series UR_GAP and PAYROLL_DIFF are computed in main() from
    #  the two hidden US series below — they get cards via SYNTHETIC_META)
    "UNRATE": {
        "description": "US unemployment rate (%, SA, monthly)",
        "blurb": "National jobless rate — the yardstick Oklahoma is measured against",
        "group": "Oklahoma vs Nation",
        "tickers": [],
        "higher_is_tailwind": False,
        "is_rate": True,
        "hidden": True,   # input to UR_GAP, no card of its own
    },
    "PAYEMS": {
        "description": "US nonfarm payrolls (thousands, SA, monthly)",
        "blurb": "National job count — input to the OK-vs-US growth comparison",
        "group": "Oklahoma vs Nation",
        "tickers": [],
        "higher_is_tailwind": True,
        "is_rate": False,
        "hidden": True,   # input to PAYROLL_DIFF, no card of its own
    },
    # National twins for the State-vs-Nation comparison table (all hidden —
    # they exist to give the Oklahoma numbers something to stand against).
    # Verified via the FRED API 2026-07-04.
    "ICSA": {
        "description": "US initial jobless claims (weekly, SA)",
        "blurb": "National claims — comparison basis for OK claims growth",
        "group": "Oklahoma vs Nation",
        "tickers": [],
        "higher_is_tailwind": False,
        "is_rate": False,
        "hidden": True,
    },
    "PERMIT": {
        "description": "US building permits (thousands, SAAR, monthly)",
        "blurb": "National permits — comparison basis for OK construction",
        "group": "Oklahoma vs Nation",
        "tickers": [],
        "higher_is_tailwind": True,
        "is_rate": False,
        "hidden": True,
    },
    "USSTHPI": {
        "description": "US house price index (FHFA all-transactions, quarterly)",
        "blurb": "National home prices — comparison basis for OK housing",
        "group": "Oklahoma vs Nation",
        "tickers": [],
        "higher_is_tailwind": True,
        "is_rate": False,
        "hidden": True,
    },
    # ----------------------------------------------------------- Energy
    "DCOILWTICO": {
        "description": "WTI spot, Cushing OK ($/bbl, daily)",
        "blurb": "Benchmark US oil price, set at Cushing OK — drives drilling, royalties, and state tax receipts",
        "group": "Energy",
        "tickers": ["DVN", "EXE", "GPOR", "MNR", "SD", "HP"],
        "higher_is_tailwind": True,
        "is_rate": False,
    },
    "DHHNGSP": {
        "description": "Henry Hub spot ($/MMBtu, daily)",
        "blurb": "Benchmark US natural gas price — revenue for producers and pipes, a cost for chemical makers",
        "group": "Energy",
        # LXU burns gas as ammonia feedstock — a higher price is a cost
        # headwind for it, opposite of the producers/pipes here. Spelled
        # out as an explicit exception in the narrative facts.
        "tickers": ["EXE", "GPOR", "OKE", "WMB", "LXU"],
        "higher_is_tailwind": True,
        "cost_side_exception": {"ticker": "LXU", "note": "feedstock cost — higher Henry Hub is a headwind for LXU, unlike the producers/pipes above"},
        "is_rate": False,
    },
    "OKNRMN": {
        "description": "OK mining & logging employment (thousands, SA, monthly)",
        "blurb": "Oil-patch jobs in Oklahoma — where energy prices become paychecks",
        "group": "Energy",
        # The energy-JOBS pulse — where WTI meets Main Street. Feeds the
        # E&P names' activity read and the banks' energy-credit picture.
        "tickers": ["DVN", "EXE", "GPOR", "MNR", "SD", "HP", "TUSK", "BOKF"],
        "higher_is_tailwind": True,
        "is_rate": False,
    },
    # --------------------------------------------------- Rates & Credit
    "DGS10": {
        "description": "10-year Treasury yield (%, daily)",
        "blurb": "The benchmark long-term interest rate — sets mortgage and muni yields and bank margins",
        "group": "Rates & Credit",
        "tickers": ["BOKF", "BANF", "BSVN", "OGE", "OGS"],
        "higher_is_tailwind": True,   # unused — context suppresses the call
        "is_rate": True,
        "context": True,  # higher yields help bank NIMs but hurt the
                          # bond-proxy utilities and muni prices — no single
                          # honest tailwind/headwind exists for this card
    },
    "T10Y3M": {
        "description": "Yield curve: 10Y minus 3M (ppt, daily)",
        "blurb": "Long rates minus short rates — below zero has preceded every modern recession",
        "group": "Rates & Credit",
        "tickers": ["BOKF", "BANF", "BSVN", "OGE", "OGS"],
        "higher_is_tailwind": True,
        "is_rate": True,
        "context": True,  # the inversion regime flag carries the judgment
    },
    "FEDFUNDS": {
        "description": "Fed funds effective rate (%, monthly)",
        "blurb": "The Fed's policy rate — the cost of money everything else prices off",
        "group": "Rates & Credit",
        "tickers": ["BOKF", "BANF", "BSVN", "OGE", "OGS"],
        "higher_is_tailwind": True,
        "is_rate": True,
        "context": True,  # policy direction cuts differently across the book
    },
    # -------------------------------------------- Housing & Construction
    "OKBPPRIVSA": {
        "description": "OK building permits, 3-mo avg (units, SA, monthly)",
        "blurb": "New housing permitted in OK — a forward look at construction and CRE demand",
        "group": "Housing & Construction",
        # Construction activity is the real-economy counterpart to the
        # banks' CRE books and the muni tax base.
        "tickers": ["BOKF", "BANF", "BSVN", "AAON"],
        "higher_is_tailwind": True,
        "is_rate": False,
        "transform": "ma3",  # small monthly counts are noisy; St. Louis Fed
                             # X-13 SA re-estimates each month anyway
    },
    "OKSTHPI": {
        "description": "OK house price index (FHFA all-transactions, quarterly)",
        "blurb": "Oklahoma home values (FHFA) — bank collateral and the property-tax base under muni credits",
        "group": "Housing & Construction",
        "tickers": ["BOKF", "BANF", "BSVN"],
        "higher_is_tailwind": True,
        "is_rate": False,
    },
    # ------------------------------------------------------ Metro Detail
    "TULS140NA": {
        "description": "Tulsa MSA nonfarm employment (thousands, SA, monthly)",
        "blurb": "Jobs in metro Tulsa — the economic base under local deposits, real estate, and muni credits",
        "group": "Metro Detail",
        "tickers": ["BOKF", "OKE", "WMB", "HP", "AAON", "MTRX"],
        "higher_is_tailwind": True,
        "is_rate": False,
    },
    "OKLA440NA": {
        "description": "OKC MSA nonfarm employment (thousands, SA, monthly)",
        "blurb": "Jobs in metro OKC — same base, capital-city side",
        "group": "Metro Detail",
        "tickers": ["PAYC", "DVN", "EXE", "BANF", "BSVN", "OGE", "LXU"],
        "higher_is_tailwind": True,
        "is_rate": False,
    },
}

# Metadata for the two synthetic OK-vs-US series built in main().
SYNTHETIC_META = {
    "UR_GAP": {
        "description": "OK minus US unemployment gap (ppt, monthly)",
        "blurb": "How Oklahoma unemployment compares to the nation — above zero means the state is doing worse than the US",
        "group": "Oklahoma vs Nation",
        "tickers": ["BOKF", "BANF", "BSVN", "PAYC"],
        # OK historically runs BELOW the nation; the gap turning positive
        # means the state is lagging the US cycle — a genuine regime event.
        "higher_is_tailwind": False,
        "is_rate": True,
    },
    "PAYROLL_DIFF": {
        "description": "OK minus US payroll growth (ppt, y/y, monthly)",
        "blurb": "Is Oklahoma adding jobs faster or slower than the country",
        "group": "Oklahoma vs Nation",
        "tickers": ["BOKF", "BANF", "PAYC", "OGE"],
        "higher_is_tailwind": True,
        "is_rate": True,
    },
}

API_URL = "https://api.stlouisfed.org/fred/series/observations"
START_DATE = "2000-01-01"          # enough history for any chart or model
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "fred"


def fetch_series(series_id: str, api_key: str) -> pd.DataFrame:
    """Fetch one series (from START_DATE) as a DataFrame of (date, value)."""
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": START_DATE,
    }
    resp = requests.get(API_URL, params=params, timeout=30)
    resp.raise_for_status()

    rows = resp.json()["observations"]
    df = pd.DataFrame(rows, columns=["date", "value"])
    df["date"] = pd.to_datetime(df["date"])
    # FRED marks missing observations (weekends/holidays on daily series)
    # with "." — coerce those to NaN and drop them.
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna(subset=["value"]).reset_index(drop=True)


def apply_transform(df: pd.DataFrame, transform: str | None) -> pd.DataFrame:
    """Smoothing for the card number; the raw series still goes to CSV."""
    if transform in ("ma3", "ma4"):
        window = int(transform[2])
        out = df.copy()
        out["value"] = out["value"].rolling(window).mean().round(2)
        return out.dropna(subset=["value"]).reset_index(drop=True)
    return df


def yoy_series(df: pd.DataFrame) -> pd.DataFrame:
    """Monthly series -> its year-over-year % growth series, matched by
    exact prior-year date (FRED monthly dates are all first-of-month)."""
    prior = df.rename(columns={"date": "match", "value": "prior"})
    cur = df.copy()
    cur["match"] = cur["date"] - pd.DateOffset(years=1)
    merged = cur.merge(prior, on="match", how="inner")
    merged["value"] = ((merged["value"] / merged["prior"] - 1) * 100).round(2)
    return merged[["date", "value"]]


def build_synthetics(fetched: dict) -> dict:
    """The two OK-vs-US series — the desk's 'is Oklahoma lagging?' frame."""
    synth = {}

    # UR_GAP: OK minus US unemployment, matched on the same month. Note the
    # timing wrinkle: state data prints ~3 weeks after the national number,
    # so the gap's latest month trails the US headline — inner join keeps
    # only months where both sides exist, which is the honest comparison.
    gap = fetched["OKUR"].merge(
        fetched["UNRATE"], on="date", suffixes=("_ok", "_us")
    )
    gap["value"] = (gap["value_ok"] - gap["value_us"]).round(2)
    synth["UR_GAP"] = gap[["date", "value"]]

    # PAYROLL_DIFF: OK payroll growth minus US payroll growth, both y/y.
    diff = yoy_series(fetched["OKNA"]).merge(
        yoy_series(fetched["PAYEMS"]), on="date", suffixes=("_ok", "_us")
    )
    diff["value"] = (diff["value_ok"] - diff["value_us"]).round(2)
    synth["PAYROLL_DIFF"] = diff[["date", "value"]]

    return synth


def latest_common(df_ok: pd.DataFrame, df_us: pd.DataFrame) -> tuple:
    """(date, ok_value, us_value) at the latest date BOTH sides have — state
    data prints ~3 weeks after the national number, so comparing each side's
    own latest would compare different months and quietly mislead."""
    merged = df_ok.merge(df_us, on="date", suffixes=("_ok", "_us"))
    row = merged.iloc[-1]
    return row["date"], row["value_ok"], row["value_us"]


def build_comparison(fetched: dict) -> list[dict]:
    """
    The State-vs-Nation table: every Oklahoma number next to its national
    twin, the gap, and a deterministic verdict. This is what turns a
    floating "4.1" into information: 4.1 vs the nation's 4.2 says more
    than any chart of 4.1 alone.
    """
    claims_ok = apply_transform(fetched["OKICLAIMS"], "ma4")
    claims_us = apply_transform(fetched["ICSA"], "ma4")
    permits_ok = apply_transform(fetched["OKBPPRIVSA"], "ma3")
    permits_us = apply_transform(fetched["PERMIT"], "ma3")

    # (indicator, ok_df, us_df, basis, lower_is_better, meaning)
    spec = [
        ("Unemployment rate", fetched["OKUR"], fetched["UNRATE"], "level", True,
         "The headline health check — OK has historically run below the US"),
        ("Payroll growth (y/y)", yoy_series(fetched["OKNA"]), yoy_series(fetched["PAYEMS"]), "growth", False,
         "Is the state adding jobs faster or slower than the country"),
        ("Jobless claims growth (y/y, 4-wk avg)", yoy_weekly(claims_ok), yoy_weekly(claims_us), "growth", True,
         "Layoff pressure vs the nation (growth comparison neutralizes the OK-NSA / US-SA mismatch)"),
        ("Building permits growth (y/y, 3-mo avg)", yoy_series(permits_ok), yoy_series(permits_us), "growth", False,
         "Construction momentum — the real-economy side of bank CRE books"),
        ("Home price growth (y/y)", yoy_series(fetched["OKSTHPI"]), yoy_series(fetched["USSTHPI"]), "growth", False,
         "Collateral and property-tax-base momentum vs the nation"),
    ]

    rows = []
    for name, ok_df, us_df, basis, lower_better, meaning in spec:
        when, ok, us = latest_common(ok_df, us_df)
        diff = round(ok - us, 2)
        threshold = 0.1 if basis == "level" else 0.3   # "inline" dead zone
        if abs(diff) <= threshold:
            verdict = "inline"
        else:
            ok_wins = (diff < 0) if lower_better else (diff > 0)
            verdict = "OK stronger" if ok_wins else "OK weaker"
        rows.append({
            "indicator": name,
            "basis": basis,
            "ok": round(ok, 2),
            "us": round(us, 2),
            "diff_ppt": diff,
            "verdict": verdict,
            "asof": when.date(),
            "meaning": meaning,
        })
    return rows


def yoy_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Weekly series -> y/y % growth via a 52-week positional shift —
    week-ending dates drift, so the exact-date match used for monthly
    series would never line up."""
    out = df.copy()
    out["value"] = ((out["value"] / out["value"].shift(52) - 1) * 100).round(2)
    return out.dropna(subset=["value"]).reset_index(drop=True)


# One forecasting model per chart tab. Two modes, chosen for how each
# projection LOOKS AND BEHAVES in the space the chart displays:
#
#   level  (unemployment): ARIMA(p,1,q) on the level itself — the one
#          model here that demonstrably beats a no-change forecast.
#   growth (the other four): ARIMA(p,0,q) WITH A CONSTANT fitted directly
#          on the displayed y/y series. An earlier design forecast the
#          level and divided by last year's actual months — statistically
#          fine, but the bumpy denominators made every projection zigzag
#          like noise. The y/y series is near-stationary, so modeling it
#          directly yields smooth paths that mean-revert toward the
#          series' own recent average, with native intervals.
#
# Orders are chosen by lowest AIC over p,q ∈ {0,1,2} — the automated
# counterpart of Box-Jenkins ACF/PACF identification (see the vault's
# Forecasting and ARIMA notes).
FORECAST_TARGETS = {
    "ur": {"source": "OKUR", "transform": None, "quarterly": False,
           "mode": "level", "horizon": 6,
           "label": "OK unemployment rate"},
    "pay": {"source": "OKNA", "transform": None, "quarterly": False,
            "mode": "growth", "horizon": 6,
            "label": "OK payroll growth (y/y)"},
    # Claims growth trains post-2022 only: the 2021 base-effect swings
    # (±700% y/y off the COVID floor) would dominate the variance and
    # blow the intervals out, exactly like the raw 2020 spike did in an
    # earlier levels-based version.
    "claims": {"source": "OKICLAIMS", "transform": "ma4", "quarterly": False,
               "mode": "growth", "horizon": 6, "train_start": "2022-07-01",
               "label": "OK jobless claims growth (y/y, 4-wk avg)",
               "caveat": "Claims are not seasonally adjusted; the y/y transform "
                         "absorbs most seasonality, but treat this one with extra skepticism.",
               "notes": "Trained on post-2022-07 data — 2021 base-effect swings "
                        "off the COVID floor would otherwise dominate the variance."},
    "permits": {"source": "OKBPPRIVSA", "transform": "ma3", "quarterly": False,
                "mode": "growth", "horizon": 6,
                "label": "OK building permits growth (y/y, 3-mo avg)"},
    "hpi": {"source": "OKSTHPI", "transform": None, "quarterly": True,
            "mode": "growth", "horizon": 2,
            "label": "OK home price growth (y/y)"},
}


def chart_level_series(df: pd.DataFrame, transform: str | None,
                       quarterly: bool) -> pd.Series:
    """The smoothed, one-point-per-period level series, frequency pinned
    (without an explicit freq statsmodels loses the forecast dates)."""
    s = df.set_index("date")["value"]
    if transform in ("ma3", "ma4"):
        s = s.rolling(int(transform[2])).mean().dropna()
    freq = "QS" if quarterly else "MS"
    s = s.groupby(pd.Grouper(freq=freq)).mean().dropna()
    return s.asfreq(freq).interpolate(limit_direction="both")


def fit_series_for(spec: dict, fetched: dict) -> pd.Series:
    """The series the model fits = the series the chart displays."""
    level = chart_level_series(fetched[spec["source"]],
                               spec["transform"], spec["quarterly"])
    if spec["mode"] == "growth":
        lag = 4 if spec["quarterly"] else 12
        series = (level.pct_change(periods=lag) * 100).dropna()
    else:
        series = level
    if spec.get("train_start"):
        series = series[series.index >= pd.Timestamp(spec["train_start"])]
    return series


def fit_best_arima(series: pd.Series, d: int, trend: str | None):
    """Lowest-AIC (p,d,q) over a small grid. Returns (fit, order)."""
    from statsmodels.tsa.arima.model import ARIMA
    best = None
    for p in range(3):
        for q in range(3):
            if d == 1 and p == 0 and q == 0:
                continue    # (0,1,0) is a pure random walk — naive itself
            try:
                fit = ARIMA(series, order=(p, d, q), trend=trend).fit()
            except Exception:  # noqa: BLE001 — a failed order just drops out
                continue
            if best is None or fit.aic < best[0].aic:
                best = (fit, (p, d, q))
    if best is None:
        raise RuntimeError("no ARIMA order converged")
    return best


def backtest(series: pd.Series, spec: dict, order: tuple,
             d: int, trend: str | None) -> dict | None:
    """
    Out-of-sample accuracy in DISPLAY space: rolling-origin backtest.
    For each of the last several origins, refit (same order, same trend)
    on data up to that point, forecast the horizon, score vs actuals.
    Reported: MAE, RMSE, 95%-band coverage (calibration — should be near
    95), and skill vs a no-change forecast. Honesty note on the page: the
    order was selected on the FULL sample, a small look-ahead advantage.
    """
    from statsmodels.tsa.arima.model import ARIMA

    H = spec["horizon"]
    n = len(series)
    max_origins = 4 if spec["quarterly"] else 8
    min_train = 20 if spec["quarterly"] else 24
    origins = [n - H - i for i in range(max_origins) if n - H - i >= min_train]
    if not origins:
        return None

    errs, naive_errs, covered, total = [], [], 0, 0
    for o in origins:
        train = series.iloc[:o]
        try:
            fit = ARIMA(train, order=order, trend=trend).fit()
        except Exception:  # noqa: BLE001 — a failed origin just drops out
            continue
        res = fit.get_forecast(H)
        mean, ci = res.predicted_mean, res.conf_int(alpha=0.05)
        naive = train.iloc[-1]
        for h in range(H):
            actual = series.iloc[o + h]
            errs.append(mean.iloc[h] - actual)
            naive_errs.append(naive - actual)
            covered += int(ci.iloc[h, 0] <= actual <= ci.iloc[h, 1])
            total += 1
    if not errs:
        return None

    mae = sum(abs(e) for e in errs) / len(errs)
    rmse = (sum(e * e for e in errs) / len(errs)) ** 0.5
    naive_mae = sum(abs(e) for e in naive_errs) / len(naive_errs)
    return {
        "mae": round(mae, 2), "rmse": round(rmse, 2),
        "coverage_pct": round(covered / total * 100),
        "naive_mae": round(naive_mae, 2),
        "vs_naive_pct": round((1 - mae / naive_mae) * 100) if naive_mae else None,
        "n_origins": len(origins), "n_scored": total, "unit": "ppt",
    }


# ---------------------------------------------------------- state rankings
# Where Oklahoma sits among all 50 states on each chart tab's indicator.
# FRED hosts every state in the same series families already in use here,
# so the crawl is 250 small requests (~2 min, weekly — well inside FRED's
# 120/min limit at the sleep below). Missing/renamed state series are
# skipped and the rank reported "of N".
STATE_ABBRS = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

RANK_SPECS = {
    "ur": {"pattern": "{st}UR", "metric": "level", "transform": None,
           "lower_is_better": True, "direction": "1 = lowest unemployment"},
    "pay": {"pattern": "{st}NA", "metric": "yoy", "transform": None,
            "lower_is_better": False, "direction": "1 = fastest payroll growth"},
    "claims": {"pattern": "{st}ICLAIMS", "metric": "yoy_weekly", "transform": "ma4",
               "lower_is_better": True, "direction": "1 = biggest drop in claims"},
    "permits": {"pattern": "{st}BPPRIVSA", "metric": "yoy", "transform": "ma3",
                "lower_is_better": False, "direction": "1 = fastest permits growth"},
    "hpi": {"pattern": "{st}STHPI", "metric": "yoy", "transform": None,
            "lower_is_better": False, "direction": "1 = fastest home-price growth"},
}


def rank_metric(df: pd.DataFrame, spec: dict) -> float | None:
    """The comparable number for one state — same transforms as the desk's
    own series, so OK is ranked on exactly what its chart shows."""
    df = apply_transform(df, spec["transform"])
    if len(df) < 2:
        return None
    if spec["metric"] == "level":
        return float(df.iloc[-1]["value"])
    if spec["metric"] == "yoy_weekly":
        if len(df) <= 52 or not df.iloc[-53]["value"]:
            return None
        return float((df.iloc[-1]["value"] / df.iloc[-53]["value"] - 1) * 100)
    latest = df.iloc[-1]
    prior = df[df["date"] <= latest["date"] - pd.Timedelta(days=364)]
    if prior.empty or not prior.iloc[-1]["value"]:
        return None
    return float((latest["value"] / prior.iloc[-1]["value"] - 1) * 100)


def state_rankings(api_key: str) -> dict:
    out = {}
    start = (date.today() - timedelta(days=560)).isoformat()  # y/y + smoothing room
    for key, spec in RANK_SPECS.items():
        vals = {}
        for st in STATE_ABBRS:
            sid = spec["pattern"].format(st=st)
            try:
                resp = requests.get(API_URL, params={
                    "series_id": sid, "api_key": api_key, "file_type": "json",
                    "observation_start": start}, timeout=30)
                resp.raise_for_status()
                df = pd.DataFrame(resp.json()["observations"],
                                  columns=["date", "value"])
                df["date"] = pd.to_datetime(df["date"])
                df["value"] = pd.to_numeric(df["value"], errors="coerce")
                v = rank_metric(df.dropna(subset=["value"]).reset_index(drop=True), spec)
                if v is not None:
                    vals[st] = v
            except Exception:  # noqa: BLE001 — a missing state series just drops out
                pass
            time.sleep(0.45)
        if "OK" not in vals or len(vals) < 25:
            print(f"rank[{key}] skipped (OK missing or only {len(vals)} states)")
            continue
        ordered = sorted(vals.items(), key=lambda kv: kv[1],
                         reverse=not spec["lower_is_better"])
        states = [st for st, _ in ordered]
        med = sorted(vals.values())[len(vals) // 2]
        out[key] = {
            "rank": states.index("OK") + 1, "n": len(vals),
            "direction": spec["direction"],
            "ok": round(vals["OK"], 2), "median": round(med, 2),
            "leader": ordered[0][0], "leader_val": round(ordered[0][1], 2),
            "trailer": ordered[-1][0], "trailer_val": round(ordered[-1][1], 2),
        }
        print(f"rank[{key}] OK #{out[key]['rank']} of {out[key]['n']}")
    return out


def run_forecasts(fetched: dict) -> None:
    """Fit all five models; write per-tab forecast CSVs + one meta JSON."""
    all_meta = {}
    for key, spec in FORECAST_TARGETS.items():
        try:
            series = fit_series_for(spec, fetched)
            d = 1 if spec["mode"] == "level" else 0
            trend = None if spec["mode"] == "level" else "c"
            fit, order = fit_best_arima(series, d, trend)
            res = fit.get_forecast(spec["horizon"])
            mean = res.predicted_mean
            ci = res.conf_int(alpha=0.05)

            out = pd.DataFrame({
                "date": [dt.date() for dt in mean.index],
                "value": mean.values.round(2),
                "lo95": ci.iloc[:, 0].values.round(2),
                "hi95": ci.iloc[:, 1].values.round(2),
            })
            out.to_csv(DATA_DIR / f"forecast_{key}.csv", index=False)

            # The audit trail: models refit every pull (coefficients AND the
            # AIC-chosen order can change as data arrives), so each run's
            # projections are appended here, dated.
            log_path = DATA_DIR / "forecast_log.csv"
            is_new = not log_path.exists()
            with log_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(["run_date", "tab", "order", "target_date",
                                "value", "lo95", "hi95"])
                for _, r in out.iterrows():
                    w.writerow([date.today(), key, f"ARIMA{order}",
                                r["date"], r["value"], r["lo95"], r["hi95"]])

            params = {k: round(v, 4) for k, v in fit.params.to_dict().items()}
            y = "Δy" if d == 1 else "y"
            terms = []
            if "const" in params:
                terms.append(f"{params['const']:+.3f}")
            terms += [f"{params[f'ar.L{i}']:+.3f}·{y}(t−{i})"
                      for i in range(1, order[0] + 1)]
            terms += [f"{params[f'ma.L{i}']:+.3f}·ε(t−{i})"
                      for i in range(1, order[2] + 1)]
            equation = f"{y}̂(t) = " + (" ".join(terms) if terms else "0")
            all_meta[key] = {
                "label": spec["label"],
                "series_id": spec["source"],
                "order": f"ARIMA{order}" + (" + constant" if trend == "c" else ""),
                "selection": "lowest AIC over p,q ∈ {0,1,2}",
                "aic": round(fit.aic, 1),
                "nobs": int(fit.nobs),
                "train_start": str(series.index[0].date()),
                "train_end": str(series.index[-1].date()),
                "equation": equation,
                "sigma2": params.get("sigma2"),
                "last_value": round(float(series.iloc[-1]), 2),
                "d_note": ("The model works on period-to-period changes "
                           "(d=1) of the rate itself." if d == 1 else
                           "The model is fitted directly on the displayed "
                           "y/y series (d=0, with a constant), so projections "
                           "mean-revert smoothly toward the series' own "
                           "recent average — no level-to-growth conversion."),
                "growth_note": "",
                "notes": spec.get("notes", ""),
                "caveat": spec.get("caveat", ""),
                "metrics": backtest(series, spec, order, d, trend),
            }
            print(f"forecast[{key}] {all_meta[key]['order']} "
                  f"AIC {all_meta[key]['aic']} -> {out.iloc[-1]['value']}")
        except Exception as exc:  # noqa: BLE001 — never kill the pull
            print(f"forecast[{key}] skipped ({exc})")
            stale = DATA_DIR / f"forecast_{key}.csv"
            if stale.exists():
                stale.unlink()

    (BASE_DIR / "data" / "fred_forecast_meta.json").write_text(
        json.dumps(all_meta, indent=1), encoding="utf-8")


def summarize(series_id: str, df: pd.DataFrame, meta: dict) -> dict:
    """Build one dashboard-card row: latest, prior, y/y, and the reading."""
    latest = df.iloc[-1]
    prior = df.iloc[-2] if len(df) > 1 else latest

    # "Year ago" = the last observation at least 364 days before the latest,
    # which works for daily, weekly, monthly, and quarterly series alike.
    cutoff = latest["date"] - pd.Timedelta(days=364)
    year_ago_df = df[df["date"] <= cutoff]
    year_ago = year_ago_df.iloc[-1] if not year_ago_df.empty else None
    year_ago_val = year_ago["value"] if year_ago is not None else None

    yoy_pct = (
        round((latest["value"] / year_ago_val - 1) * 100, 2)
        if year_ago_val not in (None, 0) else None
    )

    # Rates/spreads move in percentage POINTS year-over-year, never
    # percent-of-a-percent (3.1% -> 4.1% is "+1.0 ppt", not "+32%").
    if meta["is_rate"] and year_ago_val is not None:
        yoy_display = f"{latest['value'] - year_ago_val:+.1f} ppt"
    elif yoy_pct is not None:
        yoy_display = f"{yoy_pct:+.1f}%"
    else:
        yoy_display = "n/a"

    # Tailwind/headwind for the linked tickers — sign of the move times the
    # pre-judged economic direction. Context series opt out entirely.
    if meta.get("context"):
        reading = "context"
    else:
        reading = "flat"
        threshold = 0.05 if not meta["is_rate"] else 0.0
        moved = (latest["value"] - year_ago_val) if (meta["is_rate"] and year_ago_val is not None) \
            else (yoy_pct if yoy_pct is not None else 0)
        if abs(moved) > threshold:
            reading = "tailwind" if ((moved > 0) == meta["higher_is_tailwind"]) else "headwind"

    return {
        "series_id": series_id,
        "description": meta["description"],
        "blurb": meta.get("blurb", ""),
        "group": meta["group"],
        "tickers": meta["tickers"],
        "latest": latest["value"],
        "latest_date": latest["date"].date(),
        "prior": prior["value"],
        "change": round(latest["value"] - prior["value"], 4),
        "year_ago": year_ago_val,
        "year_ago_date": year_ago["date"].date() if year_ago is not None else None,
        "yoy_pct": yoy_pct,
        "yoy_display": yoy_display,
        "reading": reading,
        # The dashboard charts the SAME smoothed series the number uses —
        # a card must never show a 4-wk-avg number over a raw spiky line.
        "transform": meta.get("transform", ""),
    }


def regime_flags(fetched: dict, synth: dict) -> list[dict]:
    """
    Deterministic (non-LLM) regime checks — the traffic-light row. Each is
    a plain threshold on real data, computed with pandas, never generated.
    Mirrors and extends the checklist in the vault's Weekly Brief Template.
    """
    flags = []

    def last_vs_year_ago(df):
        cutoff = df.iloc[-1]["date"] - pd.Timedelta(days=364)
        prior = df[df["date"] <= cutoff]
        return (df.iloc[-1]["value"], prior.iloc[-1]["value"]) if not prior.empty else (None, None)

    okur = fetched["OKUR"].tail(4)
    flags.append({
        "label": "OK unemployment rising 3+ months",
        "on": bool(len(okur) == 4 and okur["value"].is_monotonic_increasing),
        "detail": f"last 4 readings: {', '.join(str(v) for v in okur['value'])}",
    })

    gap_now = synth["UR_GAP"].iloc[-1]["value"]
    flags.append({
        "label": "OK unemployment above US",
        "on": bool(gap_now > 0),
        "detail": f"gap {gap_now:+.1f} ppt — OK historically runs below the nation; "
                  "positive = the state is lagging the US cycle",
    })

    claims = apply_transform(fetched["OKICLAIMS"], "ma4")
    c_now, c_ago = last_vs_year_ago(claims)
    flags.append({
        "label": "Jobless claims above year-ago (4-wk avg)",
        "on": bool(c_now is not None and c_ago is not None and c_now > c_ago),
        "detail": f"4-wk avg {c_now:,.0f} vs {c_ago:,.0f} a year ago" if c_now else "insufficient history",
    })

    m_now, m_ago = last_vs_year_ago(fetched["OKNRMN"])
    flags.append({
        "label": "OK mining employment below year-ago",
        "on": bool(m_now is not None and m_ago is not None and m_now < m_ago),
        "detail": f"{m_now:.1f}k vs {m_ago:.1f}k — the energy-jobs pulse" if m_now else "insufficient history",
    })

    w_now, w_ago = last_vs_year_ago(fetched["DCOILWTICO"])
    flags.append({
        "label": "WTI below year-ago level",
        "on": bool(w_now is not None and w_ago is not None and w_now < w_ago),
        "detail": "headwind for oil-weighted E&Ps (DVN, GPOR, MNR, SD)",
    })

    curve = fetched["T10Y3M"].iloc[-1]["value"]
    flags.append({
        "label": "Yield curve inverted (10Y-3M < 0)",
        "on": bool(curve < 0),
        "detail": f"10Y-3M at {curve:+.2f} ppt — the recession signal from the "
                  "Yield Curve model card",
    })

    okna = fetched["OKNA"]
    p_now, p_ago = last_vs_year_ago(okna)
    flags.append({
        "label": "OK payrolls below year-ago level",
        "on": bool(p_now is not None and p_ago is not None and p_now < p_ago),
        "detail": "a genuine contraction signal, not just slower growth",
    })

    okphci = fetched["OKPHCI"]
    flags.append({
        "label": "OK coincident index negative m/m",
        "on": bool(len(okphci) > 1 and okphci.iloc[-1]["value"] < okphci.iloc[-2]["value"]),
        "detail": f"latest {okphci.iloc[-1]['value']:.2f} vs prior {okphci.iloc[-2]['value']:.2f}",
    })

    flags.append({
        "label": "Rig count below prior year (manual — log Fridays)",
        "on": None,  # Baker Hughes is a manual input; not computable here
        "detail": "rigcount.bakerhughes.com",
    })

    return flags


def rule_based_summary(rows: list[dict], flags: list[dict]) -> str:
    """Deterministic fallback if Ollama is unreachable — never skip the brief."""
    on_flags = [f["label"] for f in flags if f["on"] is True]
    movers = sorted(
        [r for r in rows if r["yoy_pct"] is not None and r["reading"] != "context"],
        key=lambda r: abs(r["yoy_pct"]), reverse=True,
    )[:2]
    lines = [f"{r['description']}: {r['yoy_display']}" for r in movers]
    text = "Biggest year-over-year movers — " + "; ".join(lines) + "."
    if on_flags:
        text += " Warning signs lit: " + ", ".join(on_flags) + "."
    return text


def generate_narrative(rows: list[dict], flags: list[dict],
                       compare: list[dict]) -> str:
    """
    One 'What moved / why it matters' paragraph, via Ollama — the model only
    phrases PRE-COMPUTED facts. The tailwind/headwind judgment, units, and
    every number below were computed in Python; the model may not re-derive,
    re-rank, or invent direction (an early draft let it judge direction and
    it called rising unemployment a "tailwind for banks").
    """
    compare_lines = [
        f"- {c['indicator']}: Oklahoma {c['ok']} vs US {c['us']} "
        f"({c['diff_ppt']:+.1f} gap — {c['verdict']})"
        for c in compare
    ]
    # Build one unambiguous fact line per series. Cost-side exceptions
    # (LXU pays for the gas the others sell) are resolved HERE — the
    # exception ticker is pulled out of the main list and given its own
    # line with the opposite reading. An earlier draft handed the model a
    # tailwind list containing LXU plus a footnote reversing it, and the
    # model produced a garbled "tailwind despite being a tailwind"
    # sentence. Never ask the model to resolve a contradiction.
    # Plain-English direction words — an earlier version wrote "TAILWIND"
    # and "HEADWIND" into the prose and the whole page started sounding
    # like a sell-side cliché generator.
    word = {"tailwind": "good news", "headwind": "bad news"}
    opposite = {"tailwind": "bad news", "headwind": "good news"}
    fact_lines = []
    for r in rows:
        if r["yoy_pct"] is None or r["reading"] in ("context", "flat"):
            continue
        exc = SERIES.get(r["series_id"], {}).get("cost_side_exception")
        tickers = [t for t in r["tickers"] if not (exc and t == exc["ticker"])]
        fact_lines.append(
            f"- {r['description']} ({r['group']}): {r['yoy_display']} year-over-year "
            f"— {word[r['reading']]} for {', '.join(tickers)}"
        )
        if exc:
            fact_lines.append(
                f"- The same {r['description']} move is {opposite[r['reading']]} "
                f"for {exc['ticker']} ({exc['note']})"
            )
    flag_lines = "\n".join(f"- {f['label']}: {'ON' if f['on'] else 'off'}" for f in flags)

    prompt = f"""You are writing the "What moved" section of a weekly research
brief for a one-person Oklahoma regional research desk. Below are
pre-computed facts, including whether each move is a TAILWIND or HEADWIND
for its tickers — this judgment is already correct, do not re-derive or
second-guess it, and do not invent a direction for any series not listed.
Write 4-5 sentences: LEAD with how Oklahoma compares to the nation (the
STATE VS NATION facts), then the biggest moves and which tickers they
help or hurt, respecting the given direction words, then any exception.
End with one sentence on the overall picture (expanding / softening /
mixed), based only on the warning signs given. Plain prose, no bullet
points, no preamble, no disclaimers, cite tickers by symbol, and never
use the words "tailwind" or "headwind".

STATE VS NATION:
{chr(10).join(compare_lines)}

FACTS:
{chr(10).join(fact_lines)}

REGIME FLAGS:
{flag_lines}"""
    return narrative.generate(prompt) or rule_based_summary(rows, flags)


def main() -> None:
    # Explicit path: Task Scheduler starts us in C:\Windows\System32, so
    # "find .env by walking up from the CWD" must never be the behavior.
    load_dotenv(BASE_DIR / ".env")
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        sys.exit(
            "FRED_API_KEY is not set. Get a free key at "
            "https://fredaccount.stlouisfed.org/apikeys and export it "
            "or put it in a .env file next to this script."
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Fetch ALL series before writing ANY file: if a fetch fails on a
    # network blip, we exit with last week's CSVs fully intact instead of a
    # mixed-vintage data directory. This runs unattended — atomic or nothing.
    fetched = {}
    for series_id in SERIES:
        df = fetch_series(series_id, api_key)
        if df.empty:
            sys.exit(f"{series_id}: FRED returned no usable observations — aborting, nothing written.")
        fetched[series_id] = df

    synth = build_synthetics(fetched)

    # Everything fetched cleanly — now write.
    summary_rows = []
    for series_id, df in fetched.items():
        df.to_csv(DATA_DIR / f"{series_id}.csv", index=False)
        meta = SERIES[series_id]
        if meta.get("hidden"):
            print(f"{series_id:<11} {len(df):>6} obs (hidden input)")
            continue
        card_df = apply_transform(df, meta.get("transform"))
        summary_rows.append(summarize(series_id, card_df, meta))
        print(f"{series_id:<11} {len(df):>6} obs")

    # Synthetic series still feed the regime flags and their CSVs remain
    # chartable, but they no longer get their own table rows — the
    # State-vs-Nation comparison table presents the same story better.
    for series_id, df in synth.items():
        df.to_csv(DATA_DIR / f"{series_id}.csv", index=False)
        print(f"{series_id:<11} {len(df):>6} obs (synthetic)")

    compare = build_comparison(fetched)
    compare_df = pd.DataFrame(compare)
    compare_path = BASE_DIR / "data" / "fred_compare.csv"
    compare_df.to_csv(compare_path, index=False)
    print(f"state-vs-nation table -> {compare_path}")

    # One forecast per chart tab (see FORECAST_TARGETS for the methodology).
    run_forecasts(fetched)
    legacy = DATA_DIR / "OKUR_forecast.csv"   # pre-multi-model filename
    if legacy.exists():
        legacy.unlink()

    summary = pd.DataFrame(summary_rows)
    summary_csv = summary.copy()
    summary_csv["tickers"] = summary_csv["tickers"].apply(",".join)
    summary_path = BASE_DIR / "data" / "fred_latest.csv"
    summary_csv.to_csv(summary_path, index=False)
    print(f"\nWeekly Brief summary -> {summary_path}")

    flags = regime_flags(fetched, synth)
    flags_path = BASE_DIR / "data" / "fred_flags.json"
    flags_path.write_text(json.dumps(flags, indent=1), encoding="utf-8")
    print(f"regime flags -> {flags_path} ({sum(1 for f in flags if f['on'])} on)")

    narrative_text = generate_narrative(summary_rows, flags, compare)
    narrative_path = BASE_DIR / "data" / "fred_narrative.txt"
    narrative_path.write_text(narrative_text, encoding="utf-8")
    print(f"narrative summary -> {narrative_path}")

    # Last (slowest, least critical): where OK ranks among all 50 states on
    # each chart tab's indicator — ~250 small requests, a couple of minutes.
    try:
        rankings = state_rankings(api_key)
    except Exception as exc:  # noqa: BLE001 — rankings must never kill the pull
        print(f"state rankings skipped ({exc})")
        rankings = {}
    rank_path = BASE_DIR / "data" / "fred_rankings.json"
    rank_path.write_text(json.dumps(rankings, indent=1), encoding="utf-8")
    print(f"state rankings -> {rank_path} ({len(rankings)} tabs)")


if __name__ == "__main__":
    main()
