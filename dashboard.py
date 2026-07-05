"""
dashboard.py — regenerates dashboard.html, the desk's visual interface.

The desk's written research (theses, the dated audit trail) lives in its
notes system; this page is the live cockpit. One self-contained HTML file,
no server. Every scheduled run regenerates it (see run_*.cmd) and an open
tab re-reads itself every 15 minutes.

Design rules (hard-won across several rounds of "this looks sloppy"):
  - PANELS: every section has identical anatomy — title, context line,
    body, source footer. Page capped at 1360px.
  - COLOR appears in exactly three places: the verdict column of the
    comparison table, return columns in the company tables, and lit
    warning signs. Everything else is monochrome. No cell heatmaps, no
    trend arrows, no colored sparklines.
  - NO JARGON CHROME: tailwind/headwind labels are gone from the page
    and banned from the generated prose.
  - ONE CHART, INTERACTIVE: a single panel with five OK-vs-US dataset
    tabs, rendered by Apache ECharts (vendor/echarts.min.js — the only
    dependency, vendored so everything stays offline). Real time axes
    that recompute as you zoom, crosshair tooltips covering every series
    including the projection band, wheel zoom + range slider. Replaced a
    hand-rolled SVG chart whose axes never survived contact with users.
  - Warning signs show their EVIDENCE inline, not behind a tooltip.

Safe to run by hand any time:  python dashboard.py
"""

import csv
import io
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
OUT_FILE = BASE_DIR / "dashboard.html"
# Filing alerts are read from wherever edgar_watch writes them — the same
# DESK_ALERT_DIR setting, defaulting to the local outbox.
load_dotenv(BASE_DIR / ".env")
ALERTS_DIR = Path(os.environ.get("DESK_ALERT_DIR", BASE_DIR / "outbox"))

TASKS = [
    ("EDGAR + market", "Oklahoma Desk - EDGAR watch"),
    ("FRED", "Oklahoma Desk - FRED pull"),
    ("FDIC", "Oklahoma Desk - FDIC pull"),
]

SPARK_DAYS = 730
UP, DOWN, ACCENT, NEUTRAL = "#4caf7d", "#e05c5c", "#ff7300", "#6f7a8d"
US_GRAY = "#9aa3b2"


# ------------------------------------------------------------------ shared

def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def panel(title: str, body: str, context: str = "", foot: str = "",
          span: str = "") -> str:
    ctx = f"<div class='pctx'>{context}</div>" if context else ""
    ft = f"<div class='pfoot'>{foot}</div>" if foot else ""
    style = f" style='grid-column:{span}'" if span else ""
    return (f"<div class='panel'{style}><div class='ptitle'>{title}</div>"
            f"{ctx}<div class='pbody'>{body}</div>{ft}</div>")


# ---------------------------------------------------------------- pipeline

def task_status(task_name: str) -> dict:
    out = subprocess.run(
        ["schtasks", "/query", "/tn", task_name, "/v", "/fo", "CSV"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return {"ok": False, "text": "not installed", "next": ""}
    row = next(csv.DictReader(io.StringIO(out.stdout)))
    code = row.get("Last Result", "?")
    last = row.get("Last Run Time", "?")
    nxt = row.get("Next Run Time", "?")
    if code == "267011" or last.startswith("11/30/1999"):
        return {"ok": True, "text": "not run yet", "next": nxt}
    return {"ok": code == "0",
            "text": "OK" if code == "0" else f"exit {code}", "next": nxt}


def pipeline_line() -> str:
    bits = []
    for label, task_name in TASKS:
        s = task_status(task_name)
        dot = "ok" if s["ok"] else "bad"
        bits.append(f"<span class='dot {dot}'></span>{label}: {s['text']}"
                    f" <span class='muted'>(next {s['next']})</span>")
    return "<div class='pipeline'>" + " &nbsp;·&nbsp; ".join(bits) + "</div>"


# ------------------------------------------------------------- chart data

def monthly_bucket(pts: list) -> list:
    """Any-frequency points -> one point per month (mean, last date)."""
    buckets: dict[tuple, list] = {}
    for t, v in pts:
        buckets.setdefault((t.year, t.month), []).append((t, v))
    return [(group[-1][0], sum(v for _, v in group) / len(group))
            for group in (buckets[k] for k in sorted(buckets))]


def smoothed(series_id: str, transform: str = "") -> list[tuple]:
    """Raw history with the same smoothing its card number uses."""
    rows = read_csv_rows(BASE_DIR / "data" / "fred" / f"{series_id}.csv")
    pts = [(datetime.fromisoformat(r["date"]), float(r["value"])) for r in rows]
    if transform in ("ma3", "ma4"):
        window = int(transform[2])
        pts = [(pts[i][0], sum(v for _, v in pts[i - window + 1:i + 1]) / window)
               for i in range(window - 1, len(pts))]
    return pts


def series_points(series_id: str, transform: str = "") -> list[tuple]:
    """Full-history monthly points — the chart's range buttons window it."""
    return monthly_bucket(smoothed(series_id, transform))


def yoy_points(series_id: str, transform: str = "") -> list:
    """Year-over-year % growth, matched on the exact prior-year month."""
    pts = monthly_bucket(smoothed(series_id, transform))
    by_month = {f"{t.year}-{t.month:02d}": v for t, v in pts}
    out = []
    for t, v in pts:
        prior = by_month.get(f"{t.year - 1}-{t.month:02d}")
        if prior:
            out.append((t, round((v / prior - 1) * 100, 2)))
    return out


def forecast_points(tab_key: str) -> list:
    """[(date, mean, lo95, hi95), ...] for one chart tab's model."""
    out = []
    for r in read_csv_rows(BASE_DIR / "data" / "fred" / f"forecast_{tab_key}.csv"):
        out.append((datetime.fromisoformat(r["date"]), float(r["value"]),
                    float(r.get("lo95", r["value"])), float(r.get("hi95", r["value"]))))
    return out


def jsonable(pts: list) -> list:
    return [[p[0].strftime("%Y-%m-%d")] + [round(x, 2) for x in p[1:]] for p in pts]


def duo(ok_pts: list, us_pts: list) -> list[dict]:
    return [
        {"name": "Oklahoma", "color": ACCENT, "pts": jsonable(ok_pts)},
        {"name": "US", "color": US_GRAY, "pts": jsonable(us_pts)},
    ]


def chart_payload() -> str:
    """
    The central working tool: five datasets, every one Oklahoma AGAINST the
    US — this chart exists to answer "is the state doing better or worse
    than the country", so single-line datasets don't belong here. Full
    history from 2000 is embedded; the range buttons window it in JS.
    The five tabs mirror the five rows of the comparison table above.
    """
    # "sources": the exact FRED series behind each line — id, FRED's own
    # title (as verified on fred.stlouisfed.org), and any calculation we
    # apply. Rendered under the chart so nobody has to guess what's plotted.
    payload = {
        "ur": {
            "title": "Unemployment rate", "unit": "%",
            "series": duo(series_points("OKUR"), series_points("UNRATE")),
            "forecast": jsonable(forecast_points("ur")),
            "sources": [
                {"id": "OKUR", "name": "Unemployment Rate in Oklahoma"},
                {"id": "UNRATE", "name": "Unemployment Rate"},
            ],
            "calc": "plotted as published (monthly, SA)",
            "note": "Monthly, seasonally adjusted. Orange dashes: 6-month ARIMA "
                    "projection of the Oklahoma line, with its 95% band shaded.",
        },
        "pay": {
            "title": "Payroll growth", "unit": "% y/y",
            "series": duo(yoy_points("OKNA"), yoy_points("PAYEMS")),
            "forecast": jsonable(forecast_points("pay")),
            "sources": [
                {"id": "OKNA", "name": "All Employees: Total Nonfarm in Oklahoma"},
                {"id": "PAYEMS", "name": "All Employees, Total Nonfarm"},
            ],
            "calc": "desk-computed: % change vs the same month a year earlier",
            "note": "Year-over-year change in total nonfarm jobs, monthly.",
        },
        "claims": {
            "title": "Jobless claims growth", "unit": "% y/y",
            # Same construction the forecast model fits: 4-wk avg, bucketed
            # monthly, y/y vs the same month a year earlier — the projection
            # must continue exactly the line that's plotted.
            "series": duo(yoy_points("OKICLAIMS", "ma4"), yoy_points("ICSA", "ma4")),
            "forecast": jsonable(forecast_points("claims")),
            "sources": [
                {"id": "OKICLAIMS", "name": "Initial Claims in Oklahoma"},
                {"id": "ICSA", "name": "Initial Claims"},
            ],
            "calc": "desk-computed: 4-week average, monthly, % change vs a year earlier",
            "note": "Year-over-year change in new unemployment filings, 4-week "
                    "average. Growth comparison neutralizes the OK-NSA / US-SA mismatch.",
        },
        "permits": {
            "title": "Building permits growth", "unit": "% y/y",
            "series": duo(yoy_points("OKBPPRIVSA", "ma3"), yoy_points("PERMIT", "ma3")),
            "forecast": jsonable(forecast_points("permits")),
            "sources": [
                {"id": "OKBPPRIVSA", "name": "New Private Housing Units Authorized by Building Permits for Oklahoma"},
                {"id": "PERMIT", "name": "New Privately-Owned Housing Units Authorized: Total Units"},
            ],
            "calc": "desk-computed: 3-month average, then % change vs a year earlier",
            "note": "Year-over-year change in housing units permitted, 3-month "
                    "average — construction momentum vs the nation.",
        },
        "hpi": {
            "title": "Home price growth", "unit": "% y/y",
            "series": duo(yoy_points("OKSTHPI"), yoy_points("USSTHPI")),
            "forecast": jsonable(forecast_points("hpi")),
            "sources": [
                {"id": "OKSTHPI", "name": "All-Transactions House Price Index for Oklahoma"},
                {"id": "USSTHPI", "name": "All-Transactions House Price Index for the United States"},
            ],
            "calc": "desk-computed: % change vs the same quarter a year earlier",
            "note": "Year-over-year change in the FHFA all-transactions index, "
                    "quarterly — collateral and property-tax-base momentum.",
        },
    }
    # Attach OK's national ranking per tab (computed by fred_pull's
    # 50-state crawl) so the chart can say where the state stands.
    rankings = json.loads(read_text(BASE_DIR / "data" / "fred_rankings.json") or "{}")
    for key in payload:
        payload[key]["rank"] = rankings.get(key)
    return json.dumps(payload)


def arima_details() -> str:
    """The math behind every dashed projection, one block per tab —
    a forecast nobody can inspect deserves the 'fishy' reaction it got.
    JS shows the block matching the active tab."""
    meta_txt = read_text(BASE_DIR / "data" / "fred_forecast_meta.json")
    if not meta_txt:
        return ""
    metas = json.loads(meta_txt)
    if not metas or "order" in metas:   # empty, or the old single-model format
        return ""

    blocks = ""
    for key, m in metas.items():
        fc = forecast_points(key)
        if not fc:
            continue
        proj_rows = "".join(
            f"<tr><td class='left'>{t:%b %Y}</td><td>{v:.2f}</td>"
            f"<td>{lo:.2f} – {hi:.2f}</td></tr>"
            for t, v, lo, hi in fc
        )
        caveat = f"<br><b>Caveat:</b> {m['caveat']}" if m.get("caveat") else ""
        if m.get("notes"):
            caveat = f"<br><b>Spec notes:</b> {m['notes']}" + caveat
        mt = m.get("metrics")
        if mt:
            beats = (f"{mt['vs_naive_pct']}% better than a no-change forecast"
                     if mt.get("vs_naive_pct") is not None and mt["vs_naive_pct"] >= 0
                     else f"{abs(mt.get('vs_naive_pct') or 0)}% WORSE than a no-change "
                          "forecast — treat this projection as context, not a call")
            caveat += (
                f"<br><b>Out-of-sample accuracy</b> (rolling backtest, "
                f"{mt['n_origins']} origins × full horizon, {mt['n_scored']} scored "
                f"forecasts, order fixed): MAE {mt['mae']} {mt['unit']} · RMSE "
                f"{mt['rmse']} {mt['unit']} · 95% band covered {mt['coverage_pct']}% "
                f"of actuals · {beats}. Note: the order was selected on the full "
                f"sample, so a small look-ahead advantage leaks into these numbers.")
        blocks += f"""
        <div class='fcmeta' id='fcmeta_{key}' style='display:none'>
          <b>{m['label']}</b> — <b>{m['order']}</b> on <code>{m['series_id']}</code>,
          fitted on {m['nobs']} observations ({m['train_start']} → {m['train_end']}),
          AIC {m['aic']}. Order chosen by {m['selection']} — the automated version
          of Box-Jenkins ACF/PACF identification. {m.get('d_note', '')}<br><br>
          <code>{m['equation']}</code> &nbsp; σ² = {m['sigma2']}<br><br>
          {m['growth_note']}{caveat}
          <table style='margin-top:8px; max-width:440px'><thead>
            <tr><th class='left'>Period</th><th>Forecast</th><th>95% interval</th></tr>
          </thead><tbody>{proj_rows}</tbody></table>
          <div class='muted' style='margin-top:6px'>These are deliberately simple,
          robust specs run weekly — read the mean as the drift, and take the width
          of the 95% band as seriously as the line.</div>
        </div>"""
    if not blocks:
        return ""
    return (f"<details id='arimabox'><summary>Show the model behind this tab's "
            f"projection</summary><div class='blurb' style='max-width:none; "
            f"margin-top:8px'>{blocks}</div></details>")


# ------------------------------------------------------------------- econ

def narrative_panel() -> str:
    story = read_text(BASE_DIR / "data" / "fred_narrative.txt") or ""
    rows = read_csv_rows(BASE_DIR / "data" / "fred_latest.csv")
    asof = rows[0]["latest_date"] if rows else "—"
    return panel("This week in the Oklahoma economy", story, "",
                 "Drafted by a local model from pre-computed facts — every number "
                 f"and direction is Python. Latest FRED data {asof}.",
                 span="span 2")


def warnings_panel() -> str:
    """Only what's actually wrong, with the evidence printed right there.
    Everything not lit is one muted line — no box of nine mystery dots."""
    flags = json.loads(read_text(BASE_DIR / "data" / "fred_flags.json") or "[]")
    lit = [f for f in flags if f["on"] is True]
    quiet = [f for f in flags if f["on"] is False]
    manual = [f for f in flags if f["on"] is None]

    if lit:
        body = "".join(
            f"<div class='warnrow'><span class='down'>●</span> <b>{f['label']}</b>"
            f"<div class='muted'>{f['detail']}</div></div>"
            for f in lit
        )
    else:
        body = "<p class='muted'>Nothing lit.</p>"
    if quiet:
        body += ("<div class='muted quietline'>Not lit: "
                 + " · ".join(f["label"] for f in quiet) + "</div>")
    if manual:
        body += ("<div class='muted'>Manual check: "
                 + " · ".join(f["label"] for f in manual) + "</div>")
    return panel("Warning signs", body,
                 f"{len(lit)} of {len(lit) + len(quiet)} checks lit",
                 "Plain threshold checks on the data, recomputed every pull")


def compare_panel() -> str:
    rows = read_csv_rows(BASE_DIR / "data" / "fred_compare.csv")
    if not rows:
        return ""
    body = ""
    for r in rows:
        ok, us = float(r["ok"]), float(r["us"])
        if r["basis"] == "level":
            ok_s, us_s = f"{ok:.1f}%", f"{us:.1f}%"
        else:
            ok_s, us_s = f"{ok:+.1f}%", f"{us:+.1f}%"
        verdict = r["verdict"]
        cls = {"OK stronger": "up", "OK weaker": "down"}.get(verdict, "muted")
        if verdict == "inline":
            verdict = "in line"
        body += f"""
        <tr>
          <td>{r['indicator']}<div class='muted'>as of {r['asof']}</div></td>
          <td data-v='{ok}'><b>{ok_s}</b></td>
          <td data-v='{us}'>{us_s}</td>
          <td data-v='{r['diff_ppt']}'>{float(r['diff_ppt']):+.1f} ppt</td>
          <td><span class='{cls}'>{verdict}</span></td>
          <td class='blurb'>{r['meaning']}</td>
        </tr>"""
    table = f"""<table><thead><tr>
      <th>Indicator</th><th>Oklahoma</th><th>United States</th><th>Gap</th>
      <th>Verdict</th><th class='left'>What it means</th>
    </tr></thead><tbody>{body}</tbody></table>"""
    return panel("Oklahoma vs the nation", table,
                 "Each state number beside its national twin, matched at the latest common date",
                 "Sources: BLS, FHFA, Census, DOL via FRED · click headers to sort · "
                 "state series publish ~3 weeks after the national releases, so rows "
                 "compare at the newest month both sides have reported")


def chart_panel() -> str:
    """The central working tool — see the page JS for the interactivity."""
    tabs = ("<div class='tabs'>"
            "<button class='tab active' data-key='ur'>Unemployment</button>"
            "<button class='tab' data-key='pay'>Payroll growth</button>"
            "<button class='tab' data-key='claims'>Claims growth</button>"
            "<button class='tab' data-key='permits'>Permits growth</button>"
            "<button class='tab' data-key='hpi'>Home prices</button>"
            "</div>")
    ranges = ("<div class='tabs'>"
              "<button class='rng' data-days='365'>1Y</button>"
              "<button class='rng active' data-days='730'>2Y</button>"
              "<button class='rng' data-days='1826'>5Y</button>"
              "<button class='rng' data-days='3652'>10Y</button>"
              "<button class='rng' data-days='0'>Max</button>"
              "</div>")
    body = (tabs + ranges +
            "<div id='bigchart' style='width:100%;height:380px'></div>"
            "<div id='statrow'></div>"
            "<div class='muted' id='chartnotes'></div>"
            + arima_details())
    return panel("State of the State", body,
                 "Oklahoma vs the nation, five ways. Hover for exact values, "
                 "full history to 2000, projections shown with 95% bands.",
                 "BLS, FHFA, Census, DOL via FRED")


# Series presented in the comparison table + working chart above.
COVERED_ELSEWHERE = {"OKUR", "OKNA", "OKICLAIMS", "OKBPPRIVSA", "OKSTHPI"}


def signals_panel() -> str:
    rows = read_csv_rows(BASE_DIR / "data" / "fred_latest.csv")
    body = ""
    for r in rows:
        if r["series_id"] in COVERED_ELSEWHERE:
            continue
        yoy = r.get("yoy_display", "n/a")
        affects = r.get("tickers", "").replace(",", ", ")
        body += f"""
        <tr>
          <td>{r['description']}<div class='muted'>as of {r['latest_date']}</div></td>
          <td data-v='{r['latest']}'><b>{r['latest']}</b></td>
          <td>{yoy}</td>
          <td class='blurb'>{r.get('blurb', '')}
            <div class='muted'>relevant to {affects}</div></td>
        </tr>"""
    table = f"""<table><thead><tr>
      <th>Series</th><th>Latest</th><th>y/y</th>
      <th class='left'>What it tells you</th>
    </tr></thead><tbody>{body}</tbody></table>"""
    return panel("Other Oklahoma signals", table,
                 "State-specific series with no national twin to chart against",
                 "Philly Fed, BLS, EIA, Treasury, Fed Board via FRED")


# -------------------------------------------------------------- companies

def ret_cell(val: str) -> str:
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "<td>—</td>"
    cls = "up" if v >= 0 else "down"
    return f"<td data-v='{v}'><span class='{cls}'>{v:+.1f}%</span></td>"


def sector_panels() -> str:
    rows = read_csv_rows(BASE_DIR / "data" / "market_latest.csv")
    if not rows:
        return panel("Coverage universe", "<p class='muted'>No market pull yet — "
                     "runs daily at 5 PM with the EDGAR watch.</p>")
    html = ""
    story = read_text(BASE_DIR / "data" / "market_narrative.txt")
    if story:
        html += panel("This week in the names", story,
                      f"Prices as of {rows[0]['asof']}, daily after the close",
                      "Yahoo Finance · price-only returns — the LPs (ARLP, MNR, NGL) "
                      "pay large distributions these figures don't credit")

    sectors: dict[str, list[dict]] = {}
    for r in rows:
        sectors.setdefault(r["sector"], []).append(r)

    for sector, names in sectors.items():
        body = ""
        for r in names:
            body += f"""
            <tr>
              <td><b>{r['ticker']}</b> <span class='muted'>{r['company']}</span></td>
              <td data-v='{r['price']}'>{float(r['price']):,.2f}</td>
              {ret_cell(r['r1w'])}{ret_cell(r['r1m'])}{ret_cell(r['rytd'])}{ret_cell(r['r1y'])}
              <td data-v='{r['off_52w_high_pct']}'>{float(r['off_52w_high_pct']):+.1f}%</td>
            </tr>"""
        table = f"""<table><thead><tr>
          <th>Name</th><th>Price</th><th>1W</th><th>1M</th><th>YTD</th><th>1Y</th>
          <th>vs 52w high</th>
        </tr></thead><tbody>{body}</tbody></table>"""
        html += panel(sector, table, f"{len(names)} names under active coverage",
                      "Click headers to sort")
        if sector == "Financials & Utilities":
            html += bank_panel()
    return html


# ------------------------------------------------------------------ banks

BANK_COLS = [
    ("ASSET", "Assets $B", 1),
    ("ROA", "ROA %", 2),
    ("ROE", "ROE %", 1),
    ("NIMY", "NIM %", 2),
    ("EEFFR", "Effic. %", 1),
    ("texas_ratio_pct", "Texas %", 2),
    ("dep_growth_yoy_pct", "Dep y/y %", 2),
    ("mix_cre_nonres_pct", "CRE mix %", 1),
]


def bank_table(rows: list[dict], tier: str) -> str:
    tier_rows = sorted(
        [r for r in rows if r.get("size_tier") == tier],
        key=lambda r: float(r.get("composite_score", 0)), reverse=True,
    )
    if not tier_rows:
        return ""
    body = ""
    for r in tier_rows:
        badges = ""
        if r.get("flag_texas") == "True":
            badges += "<span class='badge'>TX</span>"
        if r.get("flag_cre") == "True":
            badges += "<span class='badge'>CRE</span>"
        score = float(r.get("composite_score", 0))
        # One neutral score bar per row — ranking at a glance without a heatmap.
        cells = (f"<td>{r['bank']} {badges}</td>"
                 f"<td data-v='{score}'><span class='scorebar'>"
                 f"<span style='width:{score:.0f}%'></span></span> <b>{score:.0f}</b></td>")
        for col, _, dec in BANK_COLS:
            try:
                val = float(r[col])
                val = val / 1_000_000 if col == "ASSET" else val
                shown = f"{val:,.{dec}f}"
            except (TypeError, ValueError, KeyError):
                val, shown = 0, "—"
            cells += f"<td data-v='{val}'>{shown}</td>"
        body += f"<tr>{cells}</tr>"

    headers = "<th>Bank</th><th>Score</th>" + "".join(
        f"<th>{h}</th>" for _, h, _ in BANK_COLS)
    label = "Large — $10B+ assets" if tier == "Large" else "Small — under $10B"
    return (f"<div class='tierlabel'>{label}</div>"
            f"<table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>")


def bank_panel() -> str:
    rows = read_csv_rows(BASE_DIR / "data" / "fdic_latest.csv")
    if not rows:
        return ""
    story = read_text(BASE_DIR / "data" / "fdic_narrative.txt") or ""
    body = f"<p class='blurb' style='margin:0 0 10px'>{story}</p>"
    body += bank_table(rows, "Large") + bank_table(rows, "Small")
    return panel(
        "Bank fundamentals — 15-bank comp set",
        body,
        "Score = composite strength vs same-size peers (100 best), shown as the bar · "
        "TX / CRE tags = elevated Texas ratio or CRE concentration vs peers and above "
        "absolute floors (10% / 35%)",
        f"FDIC call reports as of {rows[0]['REPDTE']} · includes the 12 private banks "
        "the market tables can't show",
    )


# ----------------------------------------------------------------- alerts

ALERT_ROW = re.compile(
    r"^\|\s*(?P<ticker>[A-Z.]+)\s*\|\s*(?P<form>[^|]+?)\s*\|"
    r"\s*(?P<filed>[\d-]+)\s*\|\s*\[(?P<doc>[^\]]*)\]\((?P<url>[^)]+)\)\s*\|"
)


def alerts_panel() -> str:
    notes = sorted(ALERTS_DIR.glob("Filing Alerts *.md"), reverse=True)
    rows = []
    for note in notes[:10]:
        for line in note.read_text(encoding="utf-8").splitlines():
            m = ALERT_ROW.match(line)
            if m:
                rows.append(m.groupdict())
    if not rows:
        body = ("<p class='muted'>None yet — the watcher checks daily at 5 PM. "
                "First candidates: BOKF 8-K around Jul 20.</p>")
    else:
        rows.sort(key=lambda r: r["filed"], reverse=True)
        body = "<table><thead><tr><th>Ticker</th><th>Form</th><th>Filed</th>" \
               "<th class='left'>Document</th></tr></thead><tbody>" + "".join(
            f"<tr><td><b>{r['ticker']}</b></td><td>{r['form']}</td><td>{r['filed']}</td>"
            f"<td class='left'><a href='{r['url']}'>{r['doc']}</a></td></tr>"
            for r in rows[:15]) + "</tbody></table>"
    return panel("Newest EDGAR filings", body,
                 "10-K / 10-Q / 8-K / DEF 14A / S-1 across all 26 tickers",
                 "SEC EDGAR · alert notes land in the configured alerts folder")


# ------------------------------------------------------------------- page

# The interactive chart. Kept as a plain string (not an f-string) so the
# JS braces don't need escaping; CHART_DATA is substituted at build time.
CHART_JS = """
const DATA = __CHART_DATA__;
let current = 'ur', rangeDays = 730;

// ---------------------------------------------------------------- chart
// Rendered by Apache ECharts (vendor/echarts.min.js, Apache-2.0) — real
// time axes that recompute ticks as you zoom, crosshair tooltips over
// every series including the projection band, wheel zoom + range slider.
// The hand-rolled SVG chart this replaces is why the axes looked amateur.
function fmt(v) {
  if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, {maximumFractionDigits: 0});
  if (Math.abs(v) >= 100) return v.toFixed(0);
  if (Math.abs(v) >= 10) return v.toFixed(1);
  return v.toFixed(2);
}

const chartEl = document.getElementById('bigchart');
let chart = null;
if (typeof echarts === 'undefined') {
  chartEl.innerHTML = "<p class='muted' style='padding:30px'>Chart library not found: " +
    "dashboard.html must be opened from the repository folder, next to vendor/.</p>";
} else {
  chart = echarts.init(chartEl);
  window.addEventListener('resize', () => chart.resize());
}

function tooltipHtml(params, d) {
  if (!params.length) return '';
  const t = params[0].data[0];
  const dateStr = new Date(t).toLocaleDateString(undefined, {year: 'numeric', month: 'long'});
  let html = '<b>' + dateStr + '</b>';
  for (const p of params) {
    if (!p.seriesName || p.seriesName.startsWith('__')) continue;
    if (p.data[1] === null || p.data[1] === undefined) continue;
    let line = '<div><span style="color:' + p.color + '">●</span> ' +
      p.seriesName + ': <b>' + fmt(p.data[1]) + '</b> ' + d.unit;
    if (p.seriesName === 'Projection') {
      const f = d.forecast.find(q => q[0] === p.data[0]);
      if (f) line += ' <span style="color:#8b93a3">(95%: ' + fmt(f[2]) +
                     ' to ' + fmt(f[3]) + ')</span>';
    }
    html += line + '</div>';
  }
  return html;
}

let chartEndMs = Date.now();   // set per tab: last actual or projected point

function applyRange() {
  if (!chart) return;
  if (!rangeDays) { chart.dispatchAction({type: 'dataZoom', start: 0, end: 100}); return; }
  const end = chartEndMs + 14 * 86400e3;   // small pad past the last point
  chart.dispatchAction({type: 'dataZoom',
    startValue: end - (rangeDays + 200) * 86400e3, endValue: end});
}

function render(key) {
  current = key;
  const d = DATA[key];

  // ---- context, structured: four uniform labeled tiles, same every tab
  const rk = d.rank;
  const okHist = d.series[0].pts.map(p => p[1]);
  const okNow = okHist[okHist.length - 1];
  const pctile = Math.round(100 * okHist.filter(v => v <= okNow).length / okHist.length);
  const tile = (k, v, s) => '<div class="stat"><div class="k">' + k +
    '</div><div class="v">' + v + '</div><div class="s">' + s + '</div></div>';
  let tiles = tile('Oklahoma now', fmt(okNow) + ' ' + d.unit,
                   'latest reading on this tab');
  if (rk) {
    tiles += tile('National rank', '#' + rk.rank + ' of ' + rk.n, rk.direction);
    tiles += tile('State median', fmt(rk.median) + ' ' + d.unit,
                  'the middle state right now');
    tiles += tile('Range across states', rk.leader + ' ' + fmt(rk.leader_val) +
                  ' to ' + rk.trailer + ' ' + fmt(rk.trailer_val),
                  'best to worst state');
  } else {
    tiles += tile('National rank', 'n/a', 'ranking crawl missed this series — retries next pull');
  }
  tiles += tile('Vs its own history', pctile + 'th percentile',
                'of all Oklahoma readings since 2000');
  document.getElementById('statrow').innerHTML = tiles;

  // ---- one consolidated notes block instead of scattered lines
  const notes = [d.note];
  const ends = d.series.map(s => s.pts[s.pts.length - 1][0]);
  if (new Set(ends).size > 1) {
    const f = iso => new Date(iso).toLocaleDateString(undefined, {month: 'short', year: 'numeric'});
    notes.push('Lines end on different dates (' + d.series.map((s, i) =>
      s.name + ' through ' + f(ends[i])).join(', ') + ') — state data publishes ' +
      'about three weeks after the national release, not a sign Oklahoma is lagging.');
  }
  notes.push('Data: ' + d.sources.map(s =>
    '<a href="https://fred.stlouisfed.org/series/' + s.id + '" target="_blank">' +
    s.id + '</a> “' + s.name + '”').join(' vs ') + ' · ' + d.calc);
  document.getElementById('chartnotes').innerHTML = notes.join('<br>');

  // this tab's model documentation
  const box = document.getElementById('arimabox');
  if (box) {
    const meta = document.getElementById('fcmeta_' + key);
    box.style.display = meta ? '' : 'none';
    document.querySelectorAll('.fcmeta').forEach(m => m.style.display = 'none');
    if (meta) meta.style.display = '';
  }

  if (!chart) return;
  const fc = d.forecast || [];
  const series = [];

  // 95% band: invisible lower line + stacked delta area. stackStrategy
  // 'all' is required — the default ('samesign') refuses to stack a
  // negative band-bottom with the positive delta, which silently drew
  // the band from zero instead of into negative territory. The band is
  // anchored to the last actual point (zero width there) so it grows out
  // of the line instead of popping in mid-air at the first forecast.
  const baseFc = d.series[0].pts[d.series[0].pts.length - 1];
  if (fc.length) {
    series.push({name: '__lo', type: 'line', stack: 'ci', stackStrategy: 'all',
      symbol: 'none',
      data: [[baseFc[0], baseFc[1]]].concat(fc.map(p => [p[0], p[2]])),
      lineStyle: {opacity: 0}, silent: true});
    series.push({name: '__band', type: 'line', stack: 'ci', stackStrategy: 'all',
      symbol: 'none',
      data: [[baseFc[0], 0]].concat(fc.map(p => [p[0], +(p[3] - p[2]).toFixed(4)])),
      lineStyle: {opacity: 0}, areaStyle: {color: 'rgba(255,115,0,.12)'},
      silent: true});
  }

  // No end labels: the crosshair tooltip carries the values, and the
  // labels only cluttered the projection region.
  for (const s of d.series) {
    series.push({name: s.name, type: 'line', showSymbol: false, data: s.pts,
      color: s.color, lineStyle: {width: 2}});
  }
  if (fc.length) {
    series.push({name: 'Projection', type: 'line', showSymbol: false,
      data: [baseFc].concat(fc.map(p => [p[0], p[1]])), color: '#ff7300',
      lineStyle: {width: 1.8, type: 'dashed'}});
  }

  // window the range to THIS tab's data — ending months past the last
  // projected point left sparse dead space on the right.
  const allDates = d.series.flatMap(s => s.pts.map(p => p[0]))
    .concat(fc.map(p => p[0]));
  chartEndMs = Math.max(...allDates.map(x => new Date(x).getTime()));

  chart.setOption({
    backgroundColor: 'transparent',
    animationDuration: 150,
    grid: {left: 58, right: 28, top: 18, bottom: 66},
    tooltip: {trigger: 'axis', backgroundColor: '#1c2029', borderColor: '#3a4152',
      textStyle: {color: '#dfe3ea', fontSize: 12.5},
      axisPointer: {type: 'cross', label: {backgroundColor: '#2a3040', color: '#dfe3ea'}},
      formatter: params => tooltipHtml(params, d)},
    xAxis: {type: 'time', axisLine: {lineStyle: {color: '#3a4152'}},
      axisLabel: {color: '#8b93a3', hideOverlap: true}, splitLine: {show: false}},
    yAxis: {type: 'value', scale: true,
      axisLabel: {color: '#8b93a3', formatter: v => fmt(v)},
      splitLine: {lineStyle: {color: '#232836'}}},
    dataZoom: [
      {type: 'inside'},
      {type: 'slider', height: 16, bottom: 10, borderColor: '#2a3040',
       backgroundColor: '#171a21', fillerColor: 'rgba(255,115,0,.10)',
       handleStyle: {color: '#57637d'}, moveHandleStyle: {color: '#57637d'},
       textStyle: {color: '#697181'}, brushSelect: false},
    ],
    series,
  }, {notMerge: true});
  applyRange();
}

document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  render(b.dataset.key);
}));
document.querySelectorAll('.rng').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.rng').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  rangeDays = parseInt(b.dataset.days);
  applyRange();
}));
render('ur');

// click-to-sort on every table
document.querySelectorAll('table th').forEach(th => {
  let asc = false;
  th.addEventListener('click', () => {
    const i = [...th.parentNode.children].indexOf(th);
    const tb = th.closest('table').querySelector('tbody');
    const rows = [...tb.querySelectorAll('tr')];
    asc = !asc;
    rows.sort((a, b) => {
      const av = a.cells[i].dataset.v ?? a.cells[i].textContent;
      const bv = b.cells[i].dataset.v ?? b.cells[i].textContent;
      const n = parseFloat(av) - parseFloat(bv);
      return (isNaN(n) ? String(av).localeCompare(String(bv)) : n) * (asc ? 1 : -1);
    });
    rows.forEach(r => tb.appendChild(r));
  });
});
"""


def main() -> None:
    now = datetime.now()
    js = CHART_JS.replace("__CHART_DATA__", chart_payload())
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="900">
<title>Oklahoma Desk</title>
<style>
  * {{ box-sizing:border-box; }}
  body {{ background:#101216; color:#dfe3ea; font:14px/1.5 'Segoe UI',system-ui,sans-serif;
         margin:0; padding:26px 20px 40px; }}
  .page {{ max-width:1360px; margin:0 auto; }}
  h1 {{ font-size:20px; margin:0 0 4px; }} h1 span {{ color:{ACCENT}; }}
  .muted {{ color:#8b93a3; font-size:12px; }}
  .pipeline {{ color:#8b93a3; font-size:12.5px; margin:2px 0 22px; }}
  .zonehead {{ font-size:16px; font-weight:600; letter-spacing:.02em;
               border-top:1px solid #2a3040; padding-top:18px; margin:30px 0 12px; }}
  .zonehead::before {{ content:""; display:inline-block; width:9px; height:9px;
                       background:{ACCENT}; border-radius:2px; margin-right:9px; }}

  .row {{ display:grid; gap:14px; margin-bottom:14px; }}
  .panel {{ background:#171a21; border:1px solid #242a37; border-radius:10px;
            padding:14px 16px 12px; margin-bottom:14px; }}
  .row .panel {{ margin-bottom:0; }}
  .ptitle {{ font-size:14px; font-weight:600; }}
  .pctx {{ color:#8b93a3; font-size:12px; margin:1px 0 10px; }}
  .pbody {{ font-size:13.5px; }}
  .pfoot {{ color:#697181; font-size:11.5px; border-top:1px solid #232836;
            margin-top:12px; padding-top:8px; }}

  .up {{ color:{UP}; }} .down {{ color:{DOWN}; }}
  .dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:5px; }}
  .dot.ok {{ background:{UP}; }} .dot.bad {{ background:{DOWN}; }}
  .warnrow {{ padding:5px 0; }}
  .warnrow .muted {{ margin-left:16px; }}
  .quietline {{ margin-top:10px; }}
  .badge {{ font-size:9px; font-weight:700; color:#8b93a3; border:1px solid #3a4152;
            border-radius:4px; padding:1px 4px; margin-left:3px; vertical-align:middle; }}
  .tierlabel {{ font-size:11.5px; text-transform:uppercase; letter-spacing:.06em;
                color:#697181; margin:12px 0 6px; }}
  .scorebar {{ display:inline-block; width:52px; height:7px; background:#232836;
               border-radius:4px; overflow:hidden; vertical-align:middle; margin-right:6px; }}
  .scorebar span {{ display:block; height:100%; background:#57637d; }}

  table {{ border-collapse:collapse; width:100%; font-size:13px;
           font-variant-numeric:tabular-nums; }}
  th, td {{ padding:6px 9px; border-bottom:1px solid #232836; text-align:right; }}
  th {{ color:#8b93a3; font-weight:600; cursor:pointer; user-select:none;
        white-space:nowrap; font-size:12px; }}
  th:first-child, td:first-child, .left {{ text-align:left; }}
  tr:hover td {{ background:#1c2029; }}
  td .muted {{ font-size:11px; }}
  .blurb {{ text-align:left; font-size:12px; color:#aeb6c4; max-width:420px; }}

  #statrow {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
              gap:10px; margin:12px 0 10px; }}
  .stat {{ background:#1c2029; border:1px solid #242a37; border-radius:8px;
           padding:8px 12px; }}
  .stat .k {{ font-size:10.5px; text-transform:uppercase; letter-spacing:.06em;
              color:#697181; }}
  .stat .v {{ font-size:16px; font-weight:600; margin:1px 0;
              font-variant-numeric:tabular-nums; }}
  .stat .s {{ font-size:11px; color:#8b93a3; }}
  .tabs {{ margin-bottom:8px; }}
  .tab, .rng {{ background:#1c2029; color:#aeb6c4; border:1px solid #2a3040; border-radius:6px;
          font:12.5px 'Segoe UI'; padding:4px 12px; margin-right:6px; cursor:pointer; }}
  .rng {{ padding:2px 9px; font-size:11.5px; }}
  .tab.active, .rng.active {{ background:#2a3040; color:#dfe3ea; border-color:#3a4152; }}
  details summary {{ cursor:pointer; color:#8b93a3; font-size:12.5px; margin-top:10px; }}
  code {{ background:#1c2029; border:1px solid #2a3040; border-radius:4px;
          padding:2px 7px; font-size:12.5px; }}
  a {{ color:#6aa7e8; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
  #chartnotes a {{ color:inherit; font-weight:600; }}
  #chartnotes {{ line-height:1.7; }}
  footer {{ margin-top:26px; }}
</style></head><body><div class="page">

<h1><span>●</span> Oklahoma Desk <span class="muted">— generated {now:%a %Y-%m-%d %H:%M}</span></h1>
{pipeline_line()}

<div class="zonehead">Oklahoma Economy &amp; Policy</div>
<div class="row" style="grid-template-columns:2fr 1fr">
{narrative_panel()}
{warnings_panel()}
</div>
{compare_panel()}
{chart_panel()}
{signals_panel()}

<div class="zonehead">Oklahoma Companies</div>
{sector_panels()}
{alerts_panel()}

<footer class="muted">
Research theses and the dated audit trail live in the desk's notes system.
Page generated by dashboard.py after every scheduled run · logs in logs/ ·
Baker Hughes rig count stays a manual Friday input — rigcount.bakerhughes.com.
</footer>

</div>
<script src="vendor/echarts.min.js"></script>
<script>
{js}
</script>
</body></html>"""

    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"dashboard refreshed -> {OUT_FILE}")


if __name__ == "__main__":
    main()
