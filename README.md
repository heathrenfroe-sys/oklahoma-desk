# Oklahoma Desk — data scripts

Data pipelines for the **Oklahoma Desk** — a one-person regional research
desk covering Oklahoma's economy, public companies, banks, and municipal
issuers. The scripts feed a self-contained offline dashboard
(`dashboard.html`) and, optionally, write filing alerts into a notes system
such as an Obsidian vault.

| Script | Feeds | Cadence | Key needed |
| --- | --- | --- | --- |
| `fred_pull.py` | Econ Monitor weekly brief (7 FRED series) | Weekly, Friday | `FRED_API_KEY` (free) |
| `fdic_pull.py` | Bank Comps ratio tables (15 banks, 8 quarters) | Quarterly | none |
| `edgar_watch.py` | Filing alerts for the 26-ticker coverage universe | Daily (cron) | none (`EDGAR_USER_AGENT` required) |
| `market_pull.py` | Prices + returns for the 23 Active names (Yahoo chart API) | Daily, chained after the EDGAR watch | none |

## Quickstart (fresh machine)

Everything a new clone needs — one free API key and one email string, no
paid services:

```powershell
git clone https://github.com/heathrenfroe-sys/oklahoma-desk && cd oklahoma-desk
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# edit .env: FRED_API_KEY (free: https://fredaccount.stlouisfed.org/apikeys)
#            EDGAR_USER_AGENT (your name + email — SEC fair-access rule)

.venv\Scripts\python fred_pull.py      # econ series, models, rankings (~4 min)
.venv\Scripts\python fdic_pull.py      # bank call reports
.venv\Scripts\python market_pull.py    # stock prices
.venv\Scripts\python edgar_watch.py    # baselines filings (alerts start run 2)
.venv\Scripts\python dashboard.py      # regenerates dashboard.html — open it
```

A snapshot `dashboard.html` ships in the repo, so the page renders with data
before you run anything. Optional extras that degrade gracefully if absent:
**Ollama** (local LLM for the narrative paragraphs — deterministic fallback
text otherwise) and the **Obsidian vault hookup** (`DESK_ALERT_DIR`; unset,
filing alerts land in `./outbox/`).

To run it hands-off on Windows, schedule the three `run_*.cmd` wrappers
(they set the working directory and log to `logs/`):

```powershell
schtasks /create /tn "Oklahoma Desk - EDGAR watch" /sc daily  /st 17:00 /tr "%CD%\run_edgar_watch.cmd"
schtasks /create /tn "Oklahoma Desk - FRED pull"   /sc weekly /d FRI /st 12:30 /tr "%CD%\run_fred_pull.cmd"
schtasks /create /tn "Oklahoma Desk - FDIC pull"   /sc monthly /d 1 /st 09:00 /tr "%CD%\run_fdic_pull.cmd"
```

Outputs land in `data/` (gitignored — regenerable inputs; the vault notes
are the audit trail).

## What each script does

### fred_pull.py
Pulls the seven series verified in the vault note **Econ Monitor Methodology**
(`OKUR`, `OKNA`, `OKPHCI`, `DCOILWTICO`, `DHHNGSP`, `TULS140NA`, `OKLA440NA`)
via the official FRED API. Writes per-series history CSVs plus
`data/fred_latest.csv`, whose columns map 1:1 to the Weekly Brief data table
(latest / prior / change / year-ago). The Baker Hughes rig count stays a
manual input — it's an Excel workbook on rigcount.bakerhughes.com, published
Fridays at noon Central.

Why the API and not scraping: the keyless `fredgraph.csv` endpoint is
undocumented and has intermittently 403-blocked automated clients; the
official API is the documented, supported path for unattended pulls.

### narrative.py (used by both pulls)
Short analyst-style paragraphs on the dashboard are drafted by a **local
Ollama model** (default `qwen2.5:7b`, override with `OLLAMA_MODEL`) — free,
no API key, fine for scheduled runs. Two hard rules learned the hard way:
the model only *phrases* facts that Python has already computed (an early
draft let it read raw tables and it misattributed rankings and called
rising unemployment a "tailwind for banks"), and if Ollama is down or slow
the scripts fall back to a deterministic rule-based sentence — narrative
text can never break a pull.

### fdic_pull.py
Pulls 8 quarters of call-report data for the 15 banks (by FDIC cert number)
in **Bank Comps Methodology** from the BankFind Suite API. Writes the long
history plus `data/fdic_latest.csv` with the desk ratio set: ROA, ROE, NIM,
efficiency ratio (all FDIC-precomputed), plus derived deposit growth
(q/q, y/y), Texas ratio, and loan-mix percentages.

API facts baked in (verified 2026-07-03): host is `api.fdic.gov/banks`
(the documented `banks.data.fdic.gov` 301-redirects there); dollar fields are
in **thousands**; records nest under `data[].data`. Two honest caveats, also
commented in the code: the Texas ratio here uses total equity (not tangible
equity), and true CRE-concentration-to-capital needs the risk-based-capital
field — add it from the data dictionary at https://banks.data.fdic.gov/docs/
when needed.

### edgar_watch.py
Checks `data.sec.gov/submissions` for new 10-K / 10-Q / 8-K / DEF 14A / S-1
filings across all 26 coverage-universe tickers. Ticker→CIK mapping comes from
SEC's official `company_tickers.json` (cached 7 days). State lives in
`state/edgar_seen.json`; the **first run baselines silently** (no alert
flood), alerts start on run two. New filings append to one dated markdown
note — vault frontmatter, `Up: [[Coverage Universe]]`, table of links — in
`DESK_ALERT_DIR`.

SEC fair-access rules honored: declared `EDGAR_USER_AGENT` (blocked without
it) and 150 ms between requests, well under the 10 req/s cap.

## Scheduling

### On the PC (installed 2026-07-04)
The vault lives on this machine, so the watcher writes alerts straight in.
Three Task Scheduler tasks run the `run_*.cmd` wrappers (which set the
working directory and append stdout/stderr to `logs/*.log` — an unattended
job that prints to a lost console is how the Pi trader went silently dead):

| Task | Schedule | Runner |
| --- | --- | --- |
| Oklahoma Desk - EDGAR watch | daily 5:00 PM | `run_edgar_watch.cmd` |
| Oklahoma Desk - FRED pull | Fridays 12:30 PM (after the noon-CT rig count) | `run_fred_pull.cmd` |
| Oklahoma Desk - FDIC pull | 1st of each month, 9:00 AM | `run_fdic_pull.cmd` |

All three use StartWhenAvailable (missed runs fire at next opportunity) and
run only while you're logged on. Inspect with
`Get-ScheduledTask -TaskName "Oklahoma Desk*"`, check outcomes with
`Get-ScheduledTaskInfo` or the log files. FDIC data is quarterly but the
monthly run is cheap and idempotent — the script warns when the newest
quarter isn't ingested for every bank yet.

### On the Raspberry Pi (the cron design)
`edgar_watch.py` runs fine on the Pi — **but the Pi has no write path into
the vault**: the existing deploy pattern is one-way (PC → GitHub → Pi), there
is no vault sync on either machine, and SSH key auth runs PC→Pi only.
So on the Pi, leave `DESK_ALERT_DIR` unset — alerts land in `./outbox/` —
and let the PC pull them over the existing SSH trust:

```bash
# Pi crontab (crontab -e) — weekdays, 7 AM and 5 PM
0 7,17 * * 1-5  cd ~/oklahoma-desk && .venv/bin/python edgar_watch.py >> watch.log 2>&1
```

```powershell
# PC side (Task Scheduler, after the Pi runs): pull outbox -> vault, then clear
scp "<user>@<pi-ip>:~/oklahoma-desk/outbox/*.md" "C:\path\to\your\notes\Filing Alerts\"
ssh <user>@<pi-ip> "rm -f ~/oklahoma-desk/outbox/*.md"
```

Note the Pi's IP drifts on DHCP renewal (mDNS is off) — find it by MAC prefix
`88:a2:9e`, or give it a DHCP reservation in the router first. Start with the
PC schedule; move to the Pi once the reservation exists.

## Dashboard chart + forecasts

The dashboard's central chart is rendered by **Apache ECharts** (vendored at
`vendor/echarts.min.js`, Apache-2.0 — the page's only dependency, kept local
so everything works offline). Five tabs, each Oklahoma against the US, with
zoom, a range slider, and crosshair tooltips that include the projection's
95% interval.

Each tab carries its own forecast: the underlying **level** series is fit
with an AIC-selected ARIMA (p,q over 0–2, d=1) and growth tabs are converted
to y/y against already-known history. Models **refit on every pull** — both
coefficients and the selected order can change as data arrives — so each
run's projections are appended to `data/fred/forecast_log.csv`, the dated
record of what the desk's models said and when.

## Layout

```
oklahoma-desk/
├── fred_pull.py        econ monitor pull
├── fdic_pull.py        bank comps pull
├── edgar_watch.py      filing watcher (cron-friendly)
├── requirements.txt
├── .env.example        copy to .env, fill in keys
├── data/               pulled CSVs (gitignored)
├── state/              watcher state + ticker cache (gitignored)
└── outbox/             Pi-mode alert notes awaiting sync (gitignored)
```

Next step worth doing: `git init` + a GitHub remote — the PC → GitHub →
Pi `git pull` loop is the house deploy pattern (see `quant-pi`), and this
repo is résumé material.
