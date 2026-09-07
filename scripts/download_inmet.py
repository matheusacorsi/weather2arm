"""Downloads INMET's official historical-data ZIPs and refreshes the compact
Parquet files InmetProvider reads.

INMET republishes the current year's ZIP
(https://portal.inmet.gov.br/uploads/dadoshistoricos/<year>.zip) throughout
the year as new months of station data become available -- usually within
the first ~10 days of the following month. It can also revise December's
data a few days into January.

To avoid corrupting stations.parquet (which is rebuilt from scratch on every
conversion -- see scripts/convert_inmet_to_parquet.py), this script always
re-downloads every year that already has a local Parquet file, plus the
current year, plus the previous year during January. Conversion only runs
(and only then is anything written) when at least one downloaded ZIP differs
from the last run, tracked via a small hash manifest committed alongside the
Parquet files.

Usage:
    python scripts/download_inmet.py --output-dir INMET
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import List

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.convert_inmet_to_parquet import convert  # noqa: E402

BASE_URL = "https://portal.inmet.gov.br/uploads/dadoshistoricos/{year}.zip"
HASH_STATE_FILENAME = "source_hashes.json"
REQUEST_TIMEOUT = 300


def years_to_check(output_dir: Path, today: date) -> List[int]:
    years = {int(p.stem) for p in output_dir.glob("*.parquet") if p.stem.isdigit()}
    years.add(today.year)
    if today.month == 1:
        years.add(today.year - 1)
    return sorted(years)


def download_year(year: int, dest_dir: Path) -> Path | None:
    url = BASE_URL.format(year=year)
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"  {year}: download failed ({exc})")
        return None
    if resp.status_code != 200 or not resp.content:
        print(f"  {year}: not available (HTTP {resp.status_code})")
        return None
    if not resp.content.startswith(b"PK"):
        # The portal can return HTTP 200 with an HTML error/placeholder page
        # instead of a real ZIP (e.g. a maintenance page); treat that as "not
        # available yet" rather than writing garbage into the data pipeline.
        print(f"  {year}: response was not a ZIP file, skipping")
        return None
    dest = dest_dir / f"{year}.zip"
    dest.write_bytes(resp.content)
    print(f"  {year}: downloaded {len(resp.content) / 1e6:.2f} MB")
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and refresh INMET Parquet data from the official portal.")
    parser.add_argument("--output-dir", default="INMET", help="Directory holding the compact Parquet files")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "_raw_download"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True)

    try:
        years = years_to_check(output_dir, date.today())
        print(f"Checking INMET years: {years}")

        downloaded: List[Path] = []
        for year in years:
            path = download_year(year, raw_dir)
            if path is not None:
                downloaded.append(path)

        if not downloaded:
            print("No INMET ZIPs could be downloaded; leaving existing Parquet files untouched.")
            return 0

        combined_hash = hashlib.sha256()
        for path in sorted(downloaded):
            combined_hash.update(path.name.encode("utf-8"))
            combined_hash.update(hashlib.sha256(path.read_bytes()).digest())
        new_digest = combined_hash.hexdigest()

        state_path = output_dir / HASH_STATE_FILENAME
        previous_digest = None
        if state_path.exists():
            previous_digest = json.loads(state_path.read_text(encoding="utf-8")).get("combined_sha256")

        if new_digest == previous_digest:
            print("Downloaded INMET data matches the last processed version; nothing to convert.")
            return 0

        print(f"Detected new/changed INMET data across {len(downloaded)} year(s); converting to Parquet...")
        convert(raw_dir, output_dir)

        state_path.write_text(
            json.dumps({"combined_sha256": new_digest, "years": years, "generated_at": date.today().isoformat()}, indent=2),
            encoding="utf-8",
        )
        print("INMET refresh complete.")
        return 0
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
