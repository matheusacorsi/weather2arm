# Weather2ARM

Streamlit app that downloads and processes weather data for ARM software,
combining NASA POWER and INMET (Brazil) as data sources.

- `nasa_power_v2.py` — Streamlit UI (coordinate input, favorite locations,
  date range, output options, data source selection).
- `weather_sources.py` — NASA POWER client, INMET Parquet reader
  (`InmetProvider`), and the blended dataset builder.
- `INMET/` — compact Parquet snapshot of INMET's hourly station data
  (`stations.parquet` + one `<year>.parquet` per year) that `InmetProvider`
  reads at runtime.
- `scripts/convert_inmet_to_parquet.py` — converts INMET's raw CSV/ZIP
  exports into the compact Parquet files above.
- `scripts/download_inmet.py` — downloads INMET's official yearly ZIPs
  (`https://portal.inmet.gov.br/uploads/dadoshistoricos/<year>.zip`) and
  runs the conversion above when new data is detected.
- `scripts/build_timezone_data.py` — (re)builds `data/timezones_americas.geojson.gz`,
  a small Americas-only timezone lookup used to infer a location's UTC offset.
- `build_inmet_manifest.py` — writes `inmet_manifest.json`, a summary of
  station coverage used for diagnostics.

## Keeping INMET data current

`.github/workflows/inmet-monthly-refresh.yml` runs `scripts/download_inmet.py`
daily on the 1st-10th of each month (INMET typically republishes the current
year's ZIP with the prior month's data somewhere in that window), converts
any changed data to Parquet, rebuilds `inmet_manifest.json`, and commits the
result. Trigger it manually from the Actions tab (`workflow_dispatch`) to
force an immediate refresh instead of waiting for the schedule.

To refresh locally instead:

```bash
python scripts/download_inmet.py --output-dir INMET
python build_inmet_manifest.py --data-dir INMET --output inmet_manifest.json
```

## Running the app

```bash
pip install -r requirements.txt
streamlit run nasa_power_v2.py
```
