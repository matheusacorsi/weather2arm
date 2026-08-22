from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from weather_sources import InmetProvider


def build_manifest(data_dir: Path, timezone_offset_hours: int = -3) -> dict:
    provider = InmetProvider(str(data_dir), timezone_offset_hours=timezone_offset_hours)
    stations = []

    for _, station in provider.discover_station_groups().items():
        df = provider.load_station_data(station)
        if df.empty:
            coverage_start = None
            coverage_end = None
            total_records = 0
        else:
            coverage_start = df["dt_local"].min().isoformat()
            coverage_end = df["dt_local"].max().isoformat()
            total_records = int(len(df))

        stations.append(
            {
                "station_code": station.get("station_code"),
                "station_name": station.get("station_name"),
                "latitude": station.get("latitude"),
                "longitude": station.get("longitude"),
                "uf": station.get("uf"),
                "region": station.get("region"),
                "coverage_start_local": coverage_start,
                "coverage_end_local": coverage_end,
                "total_records": total_records,
            }
        )

    stations.sort(key=lambda s: (str(s.get("station_code") or ""), str(s.get("station_name") or "")))

    return {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "data_dir": str(data_dir.resolve()),
        "timezone_offset_hours": timezone_offset_hours,
        "station_count": len(stations),
        "stations": stations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an INMET station manifest from local CSV files.")
    parser.add_argument("--data-dir", default=".", help="Directory containing INMET CSV files")
    parser.add_argument("--output", default="inmet_manifest.json", help="Output manifest path")
    parser.add_argument("--tz-offset", type=int, default=-3, help="Local timezone offset in hours (default: -3)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    output = Path(args.output)

    manifest = build_manifest(data_dir=data_dir, timezone_offset_hours=args.tz_offset)
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Manifest written: {output} ({manifest['station_count']} stations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
