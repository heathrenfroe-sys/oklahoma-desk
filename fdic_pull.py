"""
fdic_pull.py — Oklahoma Desk Bank Comps data pull.

Pulls the last 8 quarters of call-report financials for the 15-bank target
list in the vault note "Bank Comps Methodology" from the FDIC BankFind Suite
API (open, no key required) and writes:

  data/fdic/fdic_history.csv   long format: one row per bank per quarter
  data/fdic_latest.csv         latest quarter, one row per bank, with the
                               desk ratio set + derived deposit growth

Usage:  python fdic_pull.py

API notes (verified 2026-07-03):
  - The documented host banks.data.fdic.gov/api 301-redirects to
    https://api.fdic.gov/banks — this script targets the new host directly.
  - Dollar fields (ASSET, DEP, ...) are reported in THOUSANDS of USD.
  - Field dictionary: https://banks.data.fdic.gov/docs/ (RIS definitions).
"""

import time
from pathlib import Path

import pandas as pd
import requests

import narrative

# Above this asset size ($000), a bank is "Large" for comp purposes. Not a
# regulatory line — just the natural break in THIS 15-bank list: BancFirst
# ($12.8B) to Stride ($5.6B) is the biggest gap in the set, so ranking
# BOKF against Great Plains on raw ROE would be comparing a $53B regional
# to a $1.9B community bank. Ranks/percentiles below are computed within
# tier, not across the whole list.
LARGE_TIER_ASSET_THRESHOLD = 10_000_000  # $10B in $000

# Metrics where a HIGHER value is the stronger bank.
HIGHER_IS_BETTER = ["ROA", "ROE", "NIMY"]
# Metrics where a LOWER value is the stronger bank.
LOWER_IS_BETTER = ["EEFFR", "texas_ratio_pct"]

# FDIC certificate numbers verified via BankFind Suite on 2026-07-03.
# The cert is the stable join key; legal names differ from street names
# (e.g. "Bank 7" with a space, "BOKF, National Association").
BANKS = {
    4214:  "BOKF, National Association",          # Tulsa       - NASDAQ: BOKF
    4063:  "MidFirst Bank",                       # OKC         - private
    8728:  "Arvest Bank",                         # Fayetteville AR - private (AR charter)
    4239:  "First United Bank and Trust Company", # Durant      - private
    27476: "BancFirst",                           # OKC         - NASDAQ: BANF
    4091:  "Stride Bank, N.A.",                   # Enid        - private (Chime partner)
    27210: "InterBank",                           # OKC         - private
    15399: "RCB Bank",                            # Claremore   - private
    23473: "First Fidelity Bank",                 # OKC         - private
    2315:  "Armstrong Bank",                      # Muskogee    - private
    4160:  "Regent Bank",                         # Tulsa       - private
    15118: "Gateway First Bank",                  # Jenks       - private
    10667: "Mabrey Bank",                         # Bixby       - private
    4147:  "Bank 7",                              # OKC         - NASDAQ: BSVN
    34207: "Great Plains National Bank",          # Elk City    - private
}

# RIS fields for the desk ratio set. Names come from the FDIC data
# dictionary; the API silently drops unknown field names, so main()
# checks what came back. A missing precomputed ratio just drops out of
# the comps table after a warning; a missing derived input (DEP, EQ,
# the loan-mix and credit fields) makes add_derived raise KeyError
# right after the warning — fail loud, nothing gets written.
FIELDS = [
    "CERT", "REPDTE",
    # size
    "ASSET",     # total assets ($000)
    "DEP",       # total deposits ($000)
    "LNLSNET",   # net loans & leases ($000)
    "EQ",        # total equity capital ($000)
    # profitability ratios (precomputed by the FDIC, annualized %)
    "ROA",       # return on assets
    "ROE",       # return on equity
    "NIMY",      # net interest margin
    "EEFFR",     # efficiency ratio
    # loan mix ($000) — shares of LNLSNET are computed below
    "LNRE",      # all real estate loans
    "LNRECONS",  # construction & development
    "LNRENRES",  # nonfarm nonresidential (core CRE)
    "LNCI",      # commercial & industrial
    "LNAG",      # agricultural production
    # credit / Texas-ratio inputs ($000)
    "NAASSET",   # nonaccrual assets
    "ORE",       # other real estate owned (foreclosed)
    "LNATRES",   # allowance for loan & lease losses
]

API_URL = "https://api.fdic.gov/banks/financials"
QUARTERS = 8                      # matches the Interview Brief trend table
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "fdic"


def fetch_bank(cert: int) -> pd.DataFrame:
    """Fetch the last QUARTERS call-report rows for one bank."""
    params = {
        "filters": f"CERT:{cert}",
        "fields": ",".join(FIELDS),
        "sort_by": "REPDTE",
        "sort_order": "DESC",
        "limit": QUARTERS,
        "format": "json",
    }
    resp = requests.get(API_URL, params=params, timeout=30)
    resp.raise_for_status()

    # Records are nested one level down: data -> [ { data: {...} }, ... ]
    records = [row["data"] for row in resp.json()["data"]]
    df = pd.DataFrame(records)
    df["bank"] = BANKS[cert]
    return df


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Add the desk's derived metrics to the long bank-quarter table."""
    df = df.sort_values(["bank", "REPDTE"]).copy()

    # Deposit growth, computed BY DATE rather than by row position:
    # positional shifts (pct_change) mislabel the comparison whenever a
    # quarter is missing from a bank's history — a gap would silently turn
    # "year-over-year" into a 5-quarter change. Matching each report date
    # to its literal prior quarter-end / prior year-end makes a gap show
    # up honestly as NaN instead.
    df["repdte_dt"] = pd.to_datetime(df["REPDTE"].astype(str), format="%Y%m%d")
    for label, offset in [
        ("qoq", pd.offsets.QuarterEnd(1)),      # 2026-03-31 -> 2025-12-31
        ("yoy", pd.DateOffset(years=1)),        # 2026-03-31 -> 2025-03-31
    ]:
        prior = df[["bank", "repdte_dt", "DEP"]].rename(
            columns={"repdte_dt": "prior_dt", "DEP": "prior_dep"}
        )
        df["prior_dt"] = df["repdte_dt"] - offset
        df = df.merge(prior, on=["bank", "prior_dt"], how="left")
        df[f"dep_growth_{label}_pct"] = (
            (df["DEP"] / df["prior_dep"] - 1) * 100
        ).round(2)
        df = df.drop(columns=["prior_dt", "prior_dep"])

    # Texas ratio = (nonaccrual assets + foreclosed real estate)
    #             / (equity capital + loan-loss allowance), as a %.
    # Note: EQ includes intangibles; a stricter version uses tangible
    # equity, which needs the intangibles field from the data dictionary.
    df["texas_ratio_pct"] = (
        (df["NAASSET"] + df["ORE"]) / (df["EQ"] + df["LNATRES"]) * 100
    ).round(2)

    # Loan mix as % of net loans — what the bank actually is.
    for col, label in [
        ("LNRE", "re"), ("LNRECONS", "constr"), ("LNRENRES", "cre_nonres"),
        ("LNCI", "ci"), ("LNAG", "ag"),
    ]:
        df[f"mix_{label}_pct"] = (df[col] / df["LNLSNET"] * 100).round(1)

    return df


def add_rankings(comps: pd.DataFrame) -> pd.DataFrame:
    """
    Add, per bank: a size tier, a percentile RANK for each ratio (computed
    within tier — comparing a $53B bank to a $1.9B one on raw numbers isn't
    meaningful), a 0-100 composite strength score, and peer-relative flags.

    This is the fix for "just rows of random numbers": every ratio gets
    read alongside where it sits versus the 15-bank set, not in isolation.
    """
    comps = comps.copy()
    comps["size_tier"] = comps["ASSET"].apply(
        lambda a: "Large" if a >= LARGE_TIER_ASSET_THRESHOLD else "Small"
    )

    strength_cols = []
    for tier, group in comps.groupby("size_tier"):
        idx = group.index
        for col in HIGHER_IS_BETTER:
            comps.loc[idx, f"pctile_{col}"] = (group[col].rank(pct=True) * 100).round(0)
        for col in LOWER_IS_BETTER:
            # Invert: the bank with the LOWEST efficiency ratio / Texas
            # ratio should still land at the 100th strength percentile.
            comps.loc[idx, f"pctile_{col}"] = ((1 - group[col].rank(pct=True)) * 100).round(0)

    strength_cols = [f"pctile_{c}" for c in HIGHER_IS_BETTER + LOWER_IS_BETTER]
    comps["composite_score"] = comps[strength_cols].mean(axis=1).round(0)

    # Risk flags = top quartile within tier AND above an absolute floor.
    # Quartile alone flags ~25% of any group by construction — the worst of
    # a perfectly healthy peer set still got a badge, which made badges
    # meaningless (9 of 15 banks were flagged in the first draft). The
    # floors are desk conventions, not regulatory lines: Texas >= 10% is an
    # early-attention level far below the ~100% distress zone; CRE >= 35%
    # of the loan book marks real concentration. Note this is loan-mix
    # concentration, not the regulatory CRE/capital screen (that needs a
    # risk-based-capital field this script doesn't pull yet — see Bank
    # Comps Methodology in the vault).
    TEXAS_FLOOR_PCT = 10.0
    CRE_MIX_FLOOR_PCT = 35.0
    for tier, group in comps.groupby("size_tier"):
        idx = group.index
        comps.loc[idx, "flag_texas"] = (
            (group["texas_ratio_pct"].rank(pct=True) >= 0.75)
            & (group["texas_ratio_pct"] >= TEXAS_FLOOR_PCT)
        )
        comps.loc[idx, "flag_cre"] = (
            (group["mix_cre_nonres_pct"].rank(pct=True) >= 0.75)
            & (group["mix_cre_nonres_pct"] >= CRE_MIX_FLOOR_PCT)
        )

    return comps


def add_trends(history: pd.DataFrame, comps: pd.DataFrame) -> pd.DataFrame:
    """Add a quarter-over-quarter direction (up/down/flat) per ratio."""
    comps = comps.copy()
    hist = history.copy()
    hist["repdte_dt"] = pd.to_datetime(hist["REPDTE"].astype(str), format="%Y%m%d")
    comps["repdte_dt"] = pd.to_datetime(comps["REPDTE"].astype(str), format="%Y%m%d")

    ratio_cols = HIGHER_IS_BETTER + LOWER_IS_BETTER
    prior = hist[["bank", "repdte_dt"] + ratio_cols].rename(
        columns={"repdte_dt": "prior_dt", **{c: f"prior_{c}" for c in ratio_cols}}
    )
    comps["prior_dt"] = comps["repdte_dt"] - pd.offsets.QuarterEnd(1)
    comps = comps.merge(prior, on=["bank", "prior_dt"], how="left")

    for col in ratio_cols:
        cur, prev = comps[col], comps[f"prior_{col}"]
        comps[f"trend_{col}"] = "flat"
        comps.loc[cur > prev, f"trend_{col}"] = "up"
        comps.loc[cur < prev, f"trend_{col}"] = "down"
        comps.loc[prev.isna(), f"trend_{col}"] = "n/a"
        comps = comps.drop(columns=[f"prior_{col}"])

    return comps.drop(columns=["repdte_dt", "prior_dt"])


def compute_highlights(comps: pd.DataFrame) -> dict:
    """
    Every fact worth mentioning in the summary, computed in pandas — argmax/
    argmin/sort, not asked of the LLM. A 7B model reading a 15-row x 9-column
    table by eye WILL misread cells (proven: an early draft had it call
    Arvest's ROE its ROA, and name Arvest the top CRE concentration when
    First Fidelity's is more than double Arvest's). Superlatives and
    rankings are Python's job; the model only phrases what's already true.
    """
    def top(df, col, n=1, ascending=False):
        return df.nlargest(n, col) if not ascending else df.nsmallest(n, col)

    highlights = {"tiers": {}}
    for tier, group in comps.groupby("size_tier"):
        best = top(group, "composite_score").iloc[0]
        worst = top(group, "composite_score", ascending=True).iloc[0]
        highlights["tiers"][tier] = {
            "best": {"bank": best["bank"], "score": best["composite_score"]},
            "worst": {"bank": worst["bank"], "score": worst["composite_score"]},
        }

    cre_top = comps.loc[comps["mix_cre_nonres_pct"].idxmax()]
    texas_top = comps.loc[comps["texas_ratio_pct"].idxmax()]
    highlights["highest_cre"] = {"bank": cre_top["bank"], "value": cre_top["mix_cre_nonres_pct"]}
    highlights["highest_texas_ratio"] = {"bank": texas_top["bank"], "value": texas_top["texas_ratio_pct"]}
    highlights["flagged_banks"] = comps.loc[
        comps["flag_texas"] | comps["flag_cre"], "bank"
    ].tolist()
    return highlights


def rule_based_summary(highlights: dict) -> str:
    """Deterministic fallback if Ollama is unreachable — never skip the summary."""
    lines = []
    for tier, facts in highlights["tiers"].items():
        lines.append(
            f"{tier} tier: {facts['best']['bank']} strongest "
            f"({facts['best']['score']:.0f}/100), {facts['worst']['bank']} weakest "
            f"({facts['worst']['score']:.0f}/100)."
        )
    lines.append(
        f"Highest CRE concentration: {highlights['highest_cre']['bank']} "
        f"({highlights['highest_cre']['value']:.1f}% of loans). "
        f"Highest Texas ratio: {highlights['highest_texas_ratio']['bank']} "
        f"({highlights['highest_texas_ratio']['value']:.1f}%)."
    )
    return " ".join(lines)


def generate_narrative(comps: pd.DataFrame) -> str:
    """
    One short analyst-style paragraph, via Ollama — but the model is only
    asked to phrase PRE-COMPUTED facts, never to derive them from a raw
    table. See compute_highlights() for why that split exists.
    """
    highlights = compute_highlights(comps)
    fact_lines = []
    for tier, facts in highlights["tiers"].items():
        fact_lines.append(
            f"- In the {tier} tier ($10B+ = Large), the strongest bank by "
            f"composite peer-relative score is {facts['best']['bank']} "
            f"({facts['best']['score']:.0f}/100); the weakest is "
            f"{facts['worst']['bank']} ({facts['worst']['score']:.0f}/100)."
        )
    fact_lines.append(
        f"- Highest CRE loan concentration (% of loan book, not % of capital): "
        f"{highlights['highest_cre']['bank']} at {highlights['highest_cre']['value']:.1f}%."
    )
    fact_lines.append(
        f"- Highest Texas ratio: {highlights['highest_texas_ratio']['bank']} "
        f"at {highlights['highest_texas_ratio']['value']:.1f}%."
    )
    if highlights["flagged_banks"]:
        fact_lines.append(
            f"- Banks flagged for elevated Texas ratio or CRE concentration vs "
            f"peers: {', '.join(highlights['flagged_banks'])}."
        )

    prompt = f"""You are a regional-bank analyst writing 3-4 sentences for a
research desk's internal dashboard. Below are pre-verified facts — do not
compute, infer, rank, or add any number not given here, and do not name any
bank or figure not listed below. Your only job is to phrase these facts into
readable prose (you may add one sentence of general context, e.g. what a
high CRE concentration or Texas ratio implies, without inventing specifics).
Plain prose, no bullet points, no preamble, no disclaimers.

FACTS:
{chr(10).join(fact_lines)}"""
    return narrative.generate(prompt) or rule_based_summary(highlights)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    frames = []

    for cert, name in BANKS.items():
        frames.append(fetch_bank(cert))
        print(f"cert {cert:>5}  {name}")
        time.sleep(0.3)  # be polite — this is a shared public API

    df = pd.concat(frames, ignore_index=True)

    # Warn loudly if the API stopped returning any requested field.
    missing = [f for f in FIELDS if f not in df.columns]
    if missing:
        print(f"\nWARNING: fields not returned by the API: {missing}")
        print("Check the data dictionary at https://banks.data.fdic.gov/docs/")

    df = add_derived(df)

    history_path = DATA_DIR / "fdic_history.csv"
    df.to_csv(history_path, index=False)

    # Latest quarter per bank -> the comps table the methodology note uses.
    latest = df.sort_values("REPDTE").groupby("bank").tail(1)

    # Comps only make sense on a single report date. If a bank's latest
    # quarter lags the others (FDIC still ingesting, or a cert that stopped
    # filing after an acquisition), say so loudly instead of quietly mixing
    # vintages in one table.
    if latest["REPDTE"].nunique() > 1:
        newest = latest["REPDTE"].max()
        laggards = latest.loc[latest["REPDTE"] != newest, "bank"].tolist()
        print(f"\nWARNING: not all banks have {newest} data yet — "
              f"stale rows in the comps table: {laggards}")
    comp_cols = [
        "bank", "CERT", "REPDTE", "ASSET", "DEP", "LNLSNET", "EQ",
        "ROA", "ROE", "NIMY", "EEFFR", "texas_ratio_pct",
        "dep_growth_qoq_pct", "dep_growth_yoy_pct",
        "mix_re_pct", "mix_constr_pct", "mix_cre_nonres_pct",
        "mix_ci_pct", "mix_ag_pct",
    ]
    comp_cols = [c for c in comp_cols if c in latest.columns]
    comps = latest[comp_cols].sort_values("ASSET", ascending=False)
    comps = add_trends(df, comps)
    comps = add_rankings(comps)
    comps = comps.sort_values("ASSET", ascending=False)
    comps_path = BASE_DIR / "data" / "fdic_latest.csv"
    comps.to_csv(comps_path, index=False)

    summary_path = BASE_DIR / "data" / "fdic_narrative.txt"
    summary_path.write_text(generate_narrative(comps), encoding="utf-8")

    print(f"\n{len(df)} bank-quarters -> {history_path}")
    print(f"latest-quarter comps -> {comps_path}")
    print(f"narrative summary -> {summary_path}")


if __name__ == "__main__":
    main()
