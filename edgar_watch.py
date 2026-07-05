"""
edgar_watch.py — Oklahoma Desk EDGAR filing watcher.

Checks SEC EDGAR for new filings from the desk's coverage universe (the 26
tickers in the vault note "Coverage Universe") and writes one dated alert
note per day, in vault markdown, whenever something new lands.

Designed to run unattended on a schedule (cron on the Raspberry Pi, or
Task Scheduler on the PC) — it is quiet when there is nothing new, keeps
its own state, and never alerts twice for the same filing.

Setup (two environment variables, or a .env file next to this script):
  EDGAR_USER_AGENT   REQUIRED by SEC fair-access policy: your name and
                     email, e.g. "Jane Analyst jane@example.com".
                     Requests without it get blocked.
  DESK_ALERT_DIR     Where alert notes go — point it at a notes system
                     (e.g. an Obsidian vault's Filing Alerts folder).
                     If unset, notes go to ./outbox (the cron-host pattern
                     — a sync step then moves them onward, see README).

Usage:  python edgar_watch.py

First run behavior: every company is "baselined" — current filings are
recorded as seen without alerting, so a fresh install doesn't flood the
vault with a decade of history. Alerts start from the second run.

SEC fair-access rules honored here: declared User-Agent, and well under
the 10 requests/second limit (we sleep 150 ms between requests).
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# The coverage universe (Active + Monitor-only), per the vault note
# "Coverage Universe" as of 2026-07-03. UNTC deregistered from SEC
# reporting (files on OTC Markets instead) — kept here in case it
# re-registers; it simply never produces alerts.
TICKERS = [
    # Energy - Upstream & Services
    "DVN", "EXE", "HP", "GPOR", "MNR", "SD", "ARLP", "TUSK", "UNTC", "EP",
    # Energy - Midstream & Downstream
    "OKE", "WMB", "PSX", "NGL", "DINO", "CVI", "LXU",
    # Financials & Utilities
    "BOKF", "BANF", "BSVN", "OGE", "OGS",
    # Technology & Industrials
    "PAYC", "AAON", "MTRX", "EDUC",
]

# Filings worth an alert. Form 4s (insider trades) are deliberately
# excluded — too noisy for a daily watcher.
FORMS_OF_INTEREST = {"10-K", "10-Q", "8-K", "DEF 14A", "S-1"}

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
REQUEST_GAP_SECONDS = 0.15        # stay well under SEC's 10 req/s cap

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STATE_FILE = STATE_DIR / "edgar_seen.json"
TICKER_CACHE = STATE_DIR / "company_tickers.json"
TICKER_CACHE_MAX_AGE = timedelta(days=7)


def get_session() -> requests.Session:
    """One session for all requests, with the SEC-required User-Agent."""
    ua = os.environ.get("EDGAR_USER_AGENT")
    if not ua:
        sys.exit(
            "EDGAR_USER_AGENT is not set. SEC requires a declared identity, "
            'e.g.:  EDGAR_USER_AGENT="Jane Analyst jane@example.com"'
        )
    session = requests.Session()
    session.headers["User-Agent"] = ua
    return session


def load_ticker_map(session: requests.Session) -> dict:
    """Map ticker -> CIK using SEC's official list, cached for 7 days."""
    fresh = (
        TICKER_CACHE.exists()
        and datetime.now() - datetime.fromtimestamp(TICKER_CACHE.stat().st_mtime)
        < TICKER_CACHE_MAX_AGE
    )
    if not fresh:
        resp = session.get(TICKER_MAP_URL, timeout=30)
        resp.raise_for_status()
        TICKER_CACHE.write_text(resp.text, encoding="utf-8")

    raw = json.loads(TICKER_CACHE.read_text(encoding="utf-8"))
    # File shape: {"0": {"cik_str": 1090012, "ticker": "DVN", "title": ...}, ...}
    return {row["ticker"]: row["cik_str"] for row in raw.values()}


def recent_filings(session: requests.Session, cik: int) -> list[dict]:
    """The 'recent' filings block for one company, as a list of dicts."""
    resp = session.get(SUBMISSIONS_URL.format(cik=cik), timeout=30)
    resp.raise_for_status()
    recent = resp.json()["filings"]["recent"]
    # EDGAR returns parallel arrays; zip them into one dict per filing.
    return [
        {
            "form": recent["form"][i],
            "accession": recent["accessionNumber"][i],
            "filed": recent["filingDate"][i],
            "document": recent["primaryDocument"][i],
        }
        for i in range(len(recent["form"]))
    ]


def filing_url(cik: int, accession: str, document: str) -> str:
    """Human-readable URL for a filing's primary document."""
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{accession.replace('-', '')}/{document}"
    )


def write_alert_note(alert_dir: Path, new_filings: list[dict]) -> Path:
    """Append today's new filings to one dated vault note (house style)."""
    today = date.today().isoformat()
    note = alert_dir / f"Filing Alerts {today}.md"

    if not note.exists():
        header = (
            "---\n"
            "type: project\n"
            "tags: [oklahoma-desk, equity, filing-alert]\n"
            "status: evergreen\n"
            f"created: {today}\n"
            f"updated: {today}\n"
            "---\n\n"
            f"# Filing Alerts {today}\n\n"
            "**Up:** [[Coverage Universe]]\n\n"
            "New EDGAR filings from the coverage universe, written by "
            "`edgar_watch.py`. Review each and, if it matters, log it in the "
            "ticker's thesis note or start an earnings note.\n\n"
            "| Ticker | Form | Filed | Document |\n"
            "| --- | --- | --- | --- |\n"
        )
        note.write_text(header, encoding="utf-8")

    with note.open("a", encoding="utf-8") as f:
        for x in new_filings:
            f.write(
                f"| {x['ticker']} | {x['form']} | {x['filed']} "
                f"| [{x['document']}]({x['url']}) |\n"
            )
    return note


def main() -> None:
    # Explicit path: scheduled runs start in C:\Windows\System32, so the
    # .env must be found relative to this script, not the process CWD.
    load_dotenv(BASE_DIR / ".env")
    session = get_session()

    alert_dir = Path(os.environ.get("DESK_ALERT_DIR", BASE_DIR / "outbox"))
    alert_dir.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    seen = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    ticker_map = load_ticker_map(session)

    new_filings = []
    for ticker in TICKERS:
        cik = ticker_map.get(ticker)
        if cik is None:
            print(f"{ticker}: not in SEC ticker map (deregistered?) -- skipped")
            continue

        time.sleep(REQUEST_GAP_SECONDS)
        # One flaky ticker must not kill the whole run: warn and move on.
        # Its seen-list is untouched, so anything missed is caught next run.
        try:
            filings = [
                x for x in recent_filings(session, cik)
                if x["form"] in FORMS_OF_INTEREST
            ]
        except requests.RequestException as exc:
            print(f"{ticker}: fetch failed ({exc}) -- will retry next run")
            continue

        key = str(cik)
        if key not in seen:
            # First sight of this company: baseline quietly, no alerts.
            seen[key] = [x["accession"] for x in filings]
            print(f"{ticker}: baselined {len(filings)} filings")
            continue

        for x in filings:
            if x["accession"] not in seen[key]:
                seen[key].append(x["accession"])
                new_filings.append(
                    {**x, "ticker": ticker, "url": filing_url(cik, x["accession"], x["document"])}
                )

    # Write the alert note BEFORE persisting state: if the note write fails,
    # state is unchanged and the same filings alert again next run (a visible
    # duplicate beats a silently lost alert on an unattended watcher).
    if new_filings:
        note = write_alert_note(alert_dir, new_filings)
        for x in new_filings:
            print(f"NEW  {x['ticker']:<5} {x['form']:<8} filed {x['filed']}")
        print(f"\n{len(new_filings)} new filing(s) -> {note}")
    else:
        print("No new filings.")

    STATE_FILE.write_text(json.dumps(seen, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
