# EGX30 Data Pipeline

Automated EGX30 daily market-data collection pipeline using Python, Playwright (headless Chromium), and GitHub Actions.

## Why this replaces the old browser extension

This project removes all extension/native-host/local-machine dependencies and runs as a standalone scheduled pipeline in GitHub Actions. No Brave, no browser extension, and no manual browser opening are required.

## Architecture

GitHub Actions schedule/manual trigger
→ `src/collect_egx30.py`
→ Headless Chromium via Playwright
→ EGX Arabic indices page extraction and validation
→ Daily + 5-minute + derived features export (CSV/JSON)
→ Data-only commit under `data/`

## Repository structure

```text
.github/workflows/daily-egx30.yml
src/collect_egx30.py
data/.gitkeep
tests/test_collector.py
requirements.txt
.gitignore
README.md
```

## Python requirements

- Python 3.12
- Dependencies from `requirements.txt`

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

## Run collector locally

```bash
python src/collect_egx30.py
```

Optional headed-browser run:

```bash
python src/collect_egx30.py --headed
```

## Run tests

```bash
pytest -q
```

## Workflow usage

- Manual run:
  - Repository
  - → Actions
  - → Daily EGX30 collection
  - → Run workflow
- Automatic schedule: Sunday–Thursday at `15:00 UTC` (safely after market close in Cairo across standard/DST).

## Required repository setting

Repository
→ Settings
→ Actions
→ General
→ Workflow permissions
→ Read and write permissions

## Output structure and files

Each session writes under:

```text
data/YYYY/MM/DD/
```

Exactly six files are generated:

- `egx30_daily_YYYY-MM-DD.csv`
- `egx30_daily_YYYY-MM-DD.json`
- `egx30_5minute_YYYY-MM-DD.csv`
- `egx30_5minute_YYYY-MM-DD.json`
- `egx30_features_YYYY-MM-DD.csv`
- `egx30_features_YYYY-MM-DD.json`

Encoding:

- CSV: UTF-8 with BOM
- JSON: UTF-8, indented
- Arabic text preserved (`ensure_ascii=False`)

## Output schemas

### Daily fields

`Date, Open, High, Low, Close, ChangePercent, YTDPercent, IndexValueUSD, ChangePercentUSD, TotalTradedValueEGP, TotalTradedValueMarketPercent, TotalVolume, TotalVolumeMarketPercent, TradesCount, TradesMarketPercent, MarketCapEGP, MarketCapMarketPercent, Source, CollectedAt`

### Five-minute fields

`Timestamp, RawChartTime, NormalizedTime24h, Value, IntervalMinutes, Source, CollectedAt`

### Features fields

`Timestamp, RawChartTime, NormalizedTime24h, Value, PreviousValue, PointChange, Return5mPercent, Direction, SessionOpen, ChangeFromOpen, ChangeFromOpenPercent, RunningHigh, RunningLow, DistanceFromRunningHigh, DistanceFromRunningLow, PositionInObservedRangePercent, MovingAverage3, MovingAverage6, RollingStd3, RollingStd6, MinutesFromOpen, Hour, Minute, IntervalMinutes, Source, DataQualityStatus`

Feature files are mathematically derived from five-minute points and are not separate raw EGX source data.

## Validation and safety

Collector rejects output when required values are missing/invalid, chart points are empty, date parsing fails, selected index is not EGX30, or OHLC checks fail.

No output files are written until full validation passes.

## Automatic commit behavior

Workflow stages only `data/`, commits only when data changed, rebases before push, and uses GitHub Actions bot identity.

Commit message format:

`data: add EGX30 session YYYY-MM-DD`

## Diagnostics and failure artifacts

On failed collection attempts, diagnostics are written under `debug/egx30/` (error text + screenshot + page HTML when available). Workflow uploads these artifacts for 7 days only when the job fails.

## Common failure causes

- EGX page layout/selectors changed
- EGX website temporary outage or anti-bot behavior
- Delayed/partial chart point loading

## Updating selectors if EGX changes

Update extraction/label mapping in:

- `extract_raw_capture`
- `build_daily_summary`
- `build_chart_points`

Then run tests and trigger a manual workflow run.

## External source caveat

The EGX website remains the external source of truth. Changes on `https://www.egx.com.eg/ar/Indices.aspx` may break extraction and require selector/parser updates.

## Chart-time conversion assumption

Chart labels are treated as 12-hour values when meridiem markers are present (`AM/PM`, `ص/م`). If missing, conversion is inferred to maintain chronological order and align with EGX daytime session behavior.
