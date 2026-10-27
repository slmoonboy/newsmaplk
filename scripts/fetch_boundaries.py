#!/usr/bin/env python3
"""Refresh the Sri Lanka district boundaries used by the map.

Downloads the geoBoundaries LKA ADM2 "simplified" GeoJSON (ODbL 1.0) and
rewrites ``frontend/sri-lanka-districts.geojson`` in the small, normalised
shape the frontend expects:

    {
      "type": "FeatureCollection",
      "name": "Sri Lanka districts (25)",
      "attribution": "...",
      "source": "https://www.geoboundaries.org/api/current/gbOpen/LKA/ADM2/",
      "features": [
        {"type": "Feature",
         "properties": {"name": "Colombo", "iso": "LK-11", "id": "..."},
         "geometry": {"type": "MultiPolygon", "coordinates": [...]}}
      ]
    }

Only the Python standard library is used, so the script runs anywhere the
backend runs, without installing extra packages.

Usage::

    python scripts/fetch_boundaries.py
    python scripts/fetch_boundaries.py --check      # verify, do not write
    python scripts/fetch_boundaries.py --output /tmp/districts.geojson

Attribution (required when redistributing): boundaries © geoBoundaries
(ODbL 1.0), data © OpenStreetMap contributors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

API_URL = "https://www.geoboundaries.org/api/current/gbOpen/LKA/ADM2/"
USER_AGENT = "LankaPulse/1.1 (open-source news map; boundary refresh script)"
TIMEOUT_SECONDS = 60
DECIMALS = 5  # ~1 m precision; plenty for district outlines
EXPECTED_FEATURES = 25

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT_DIR / "frontend" / "sri-lanka-districts.geojson"

ATTRIBUTION = (
    "Boundaries © geoBoundaries (ODbL 1.0), "
    "data © OpenStreetMap contributors"
)


def fetch_json(url: str) -> dict:
    """GET a JSON document with a descriptive User-Agent."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def round_coords(value):
    """Recursively round every coordinate to DECIMALS places."""
    if isinstance(value, (int, float)):
        return round(float(value), DECIMALS)
    if isinstance(value, list):
        return [round_coords(item) for item in value]
    return value


def _district_name(properties: dict) -> str:
    """Return a bare district name, e.g. ``Colombo`` not ``Colombo District``."""
    name = (properties.get("shapeName") or "").strip()
    suffix = " District"
    if name.endswith(suffix):
        name = name[: -len(suffix)].strip()
    return name


def normalise(source: dict) -> dict:
    """Convert a raw geoBoundaries FeatureCollection to LankaPulse's shape."""
    raw_features = source.get("features") or []
    if len(raw_features) != EXPECTED_FEATURES:
        raise ValueError(
            f"expected {EXPECTED_FEATURES} districts, got {len(raw_features)}"
        )

    features = []
    for feature in raw_features:
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError(f"unsupported geometry {geometry.get('type')!r}")

        coordinates = geometry.get("coordinates") or []
        if geometry["type"] == "Polygon":
            # Promote single polygons so the frontend only handles MultiPolygon.
            coordinates = [coordinates]

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "name": _district_name(properties),
                    "iso": (properties.get("shapeISO") or "").strip(),
                    "id": (properties.get("shapeID") or "").strip(),
                },
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": round_coords(coordinates),
                },
            }
        )

    features.sort(key=lambda item: item["properties"]["name"])
    missing = [f["properties"]["name"] or "?" for f in features if not f["properties"]["name"]]
    if missing:
        raise ValueError(f"features without a district name: {missing}")

    return {
        "type": "FeatureCollection",
        "name": "Sri Lanka districts (25)",
        "attribution": ATTRIBUTION,
        "source": API_URL,
        "features": features,
    }


def write_atomic(path: Path, document: dict) -> None:
    """Write JSON to a temporary file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=".districts-", suffix=".geojson", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp_name)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"destination file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="download and validate, but do not overwrite the output file",
    )
    args = parser.parse_args(argv)

    print(f"→ metadata  {API_URL}")
    try:
        metadata = fetch_json(API_URL)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"✗ could not read geoBoundaries metadata: {exc}", file=sys.stderr)
        return 1

    download_url = metadata.get("simplifiedGeometryGeoJSON") or metadata.get(
        "gjDownloadURL"
    )
    if not download_url:
        print("✗ metadata has no GeoJSON download URL", file=sys.stderr)
        return 1

    print(f"→ geometry  {download_url}")
    try:
        source = fetch_json(download_url)
        document = normalise(source)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"✗ could not build boundaries: {exc}", file=sys.stderr)
        return 1

    names = [feature["properties"]["name"] for feature in document["features"]]
    print(f"✓ {len(names)} districts: {', '.join(names)}")

    if args.check:
        print("✓ check passed; output not modified")
        return 0

    write_atomic(args.output, document)
    size_kb = args.output.stat().st_size / 1024
    print(f"✓ wrote {args.output} ({size_kb:.0f} KB)")
    print(f"  attribution: {document['attribution']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
