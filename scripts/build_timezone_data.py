"""One-off maintenance script: build the bundled Americas timezone boundary file.

Downloads the current-day, no-oceans timezone boundary release from
evansiroky/timezone-boundary-builder, keeps only America/* polygons, simplifies
their geometry, and writes a small gzip-compressed GeoJSON used by
`nasa_power_v2.py` for offline UTC-offset detection.

Timezone boundaries essentially never change, so this is meant to be re-run
manually and rarely (e.g. once a year), not as part of any CI workflow.

Usage: python scripts/build_timezone_data.py
"""

from __future__ import annotations

import gzip
import io
import json
import urllib.request
import zipfile
from pathlib import Path

from shapely.geometry import mapping, shape

RELEASE_URL = "https://github.com/evansiroky/timezone-boundary-builder/releases/latest/download/timezones-now.geojson.zip"
SIMPLIFY_TOLERANCE_DEGREES = 0.005  # ~500m, plenty of margin for picking an integer UTC offset
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "timezones_americas.geojson.gz"


def main() -> None:
    print(f"Downloading {RELEASE_URL} ...")
    with urllib.request.urlopen(RELEASE_URL) as resp:
        raw_zip = resp.read()

    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        member = next(n for n in zf.namelist() if n.endswith(".json"))
        data = json.loads(zf.read(member))

    americas = [f for f in data["features"] if f["properties"].get("tzid", "").startswith("America/")]
    print(f"Filtered {len(data['features'])} world features -> {len(americas)} America/* features")

    simplified_features = []
    for feat in americas:
        geom = shape(feat["geometry"]).simplify(SIMPLIFY_TOLERANCE_DEGREES)
        simplified_features.append(
            {
                "type": "Feature",
                "properties": {"tzid": feat["properties"]["tzid"]},
                "geometry": mapping(geom),
            }
        )

    out = {"type": "FeatureCollection", "features": simplified_features}
    raw_bytes = json.dumps(out).encode("utf-8")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUTPUT_PATH, "wb", compresslevel=9) as fh:
        fh.write(raw_bytes)

    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size / 1e6:.2f} MB gzip, {len(raw_bytes) / 1e6:.2f} MB raw)")


if __name__ == "__main__":
    main()
