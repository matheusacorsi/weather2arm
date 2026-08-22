"""Converts raw INMET CSV/ZIP exports into the compact Parquet files InmetProvider reads.

Keeps only the 5 variables the app actually uses (precipitation, temperature,
humidity, wind direction, wind speed) instead of the raw files' 19 columns,
and stores them as typed columnar Parquet instead of ';'-delimited text.
On the current INMET/2026.zip this shrinks ~55MB -> ~8MB with no loss of any
data the app reads.

Usage:
    python scripts/convert_inmet_to_parquet.py --input-dir INMET --output-dir INMET

Run this whenever new raw INMET ZIPs/CSVs are dropped into --input-dir (e.g. by
the monthly refresh workflow), then delete the raw source files once the
Parquet output looks correct.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from weather_sources import RawInmetCsvSource  # noqa: E402


def convert(input_dir: Path, output_dir: Path) -> None:
    source_reader = RawInmetCsvSource(str(input_dir))
    sources = source_reader.list_inmet_sources()
    if not sources:
        print(f"No INMET CSV/ZIP sources found under {input_dir}")
        return

    stations: dict[str, dict] = {}
    year_frames: dict[int, list[pd.DataFrame]] = {}

    for i, source in enumerate(sources, start=1):
        try:
            meta, df = source_reader.parse_source(source)
        except Exception as exc:
            print(f"  skipped {source_reader._source_id(source)}: {exc}")
            continue

        station_code = meta.get("station_code") or meta.get("station_name")
        if station_code and station_code not in stations:
            stations[station_code] = {
                "station_code": station_code,
                "station_name": meta.get("station_name"),
                "latitude": meta.get("latitude"),
                "longitude": meta.get("longitude"),
                "altitude": meta.get("altitude"),
                "region": meta.get("region"),
                "uf": meta.get("uf"),
            }

        if df.empty:
            continue
        df = df.copy()
        df.insert(1, "station_code", station_code)

        for year, year_df in df.groupby(df["dt_utc"].dt.year):
            year_frames.setdefault(int(year), []).append(year_df)

        if i % 50 == 0:
            print(f"  parsed {i}/{len(sources)} sources")

    output_dir.mkdir(parents=True, exist_ok=True)

    stations_df = pd.DataFrame(stations.values())
    stations_path = output_dir / "stations.parquet"
    stations_df.to_parquet(stations_path, index=False, compression="gzip")
    print(f"Wrote {stations_path} ({len(stations_df)} stations, {stations_path.stat().st_size / 1e6:.2f} MB)")

    for year in sorted(year_frames):
        combined = pd.concat(year_frames[year], ignore_index=True)
        combined = combined.sort_values(["station_code", "dt_utc"]).reset_index(drop=True)
        year_path = output_dir / f"{year}.parquet"
        combined.to_parquet(year_path, index=False, compression="gzip")
        print(f"Wrote {year_path} ({len(combined)} rows, {year_path.stat().st_size / 1e6:.2f} MB)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert raw INMET CSV/ZIP exports to compact Parquet files.")
    parser.add_argument("--input-dir", default="INMET", help="Directory containing raw INMET .zip/.csv files")
    parser.add_argument("--output-dir", default="INMET", help="Directory to write <year>.parquet and stations.parquet into")
    args = parser.parse_args()

    convert(Path(args.input_dir), Path(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
