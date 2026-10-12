# EGX30 Automated Data Pipeline

This repository collects EGX30 market data from the Egyptian Exchange website using a scheduled GitHub Actions workflow and a standalone Python + Playwright collector.

## What this project does

- Opens `https://www.egx.com.eg/ar/Indices.aspx` in headless Chromium.
- Ensures EGX30 is selected.
- Extracts daily summary fields and 5-minute chart points.
- Validates data quality and rejects invalid/empty collection results.
- Generates derived feature rows from 5-minute points.
- Exports CSV + JSON files under `data/YYYY/MM/DD/`.
- Commits only new/updated `data/` files automatically from GitHub Actions.

## Repository structure

```text
.github/workflows/daily-egx30.yml   Scheduled/manual automation workflow
src/collect_egx30.py                Standalone EGX30 collector and processor
data/YYYY/MM/DD/                    Daily generated outputs
requirements.txt                    Python dependencies
```

## Local installation

```bash
git clone https://github.com/mohamedgamil27/discord-meeting.git
cd discord-meeting
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

## Local execution

```bash
source .venv/bin/activate
python src/collect_egx30.py --output-root data --diagnostics-dir diagnostics --retries 3 --retry-delay 20
```

## GitHub Actions execution

### Manual run

1. Open **Actions** in GitHub.
2. Select **Daily EGX30 Pipeline**.
3. Click **Run workflow**.

### Automatic schedule

The workflow runs automatically Sunday through Thursday at `13:30 UTC`.

## Output structure

For each trading day:

```text
data/YYYY/MM/DD/
  egx30_daily_YYYY-MM-DD.csv
  egx30_daily_YYYY-MM-DD.json
  egx30_5minute_YYYY-MM-DD.csv
  egx30_5minute_YYYY-MM-DD.json
  egx30_features_YYYY-MM-DD.csv
  egx30_features_YYYY-MM-DD.json
```

Notes:
- CSV files are written as UTF-8 with BOM.
- JSON files are UTF-8 with indentation.
- Feature files are mathematically derived from 5-minute points and are **not** separate raw EGX data.

## Required GitHub Actions permissions

Repository Actions must allow workflow runs to write repository contents with `GITHUB_TOKEN` (workflow uses `permissions: contents: write`).

## Troubleshooting failed workflow runs

- Check the failed run logs in the **Actions** tab.
- On collector failure, diagnostics are uploaded as an artifact (`egx30-diagnostics-<run_id>`) for 7 days.
- Diagnostics include error log, full-page screenshot, and returned HTML to help fix selector or site-layout issues.
- If the EGX site is temporarily unavailable, rerun manually after the site recovers.

