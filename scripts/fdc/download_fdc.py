#!/usr/bin/env python3
"""Download the latest Food Data Central CSV datasets and extract them
into a source folder, one subdirectory per dataset.

FDC download URLs are date-versioned (e.g. ..._foundation_food_csv_
2026-04-30.zip), so this script scrapes the official download page and
picks the newest CSV zip for each requested dataset instead of hardcoding
dates that go stale.

Result layout (default --output ./source):
    source/
      foundation/   food.csv, food_nutrient.csv, nutrient.csv, ...
      sr_legacy/    ...
      fndds/        ...            (USDA "Survey" data type)

Feed the folder straight to the converter:
    python fdc_to_ont_csv.py \
        --input source/foundation --input source/sr_legacy \
        --input source/fndds --output ../../out/fdc

Usage:
    python download_fdc.py                        # foundation + sr_legacy + fndds
    python download_fdc.py --datasets fndds       # just FNDDS (survey)
    python download_fdc.py --datasets foundation sr_legacy fndds branded  # + branded
    python download_fdc.py --output /data/fdc --keep-zips

Branded is opt-in: ~2M foods (~428 MB zip, ~2.9 GB extracted) - it far
exceeds the Supabase free-tier database size, so it is not downloaded
by default.

Only the standard library is used (urllib, zipfile). Note FNDDS/survey is
large (~200 MB zip, ~1.6 GB extracted).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

DOWNLOAD_PAGE = "https://fdc.nal.usda.gov/download-datasets"
BASE = "https://fdc.nal.usda.gov"

# dataset key -> substring identifying its CSV zip on the page.
# "fndds" is USDA's Survey (FNDDS) data type - accepted under both names.
DATASETS = {
    "foundation": "foundation_food_csv",
    "sr_legacy": "sr_legacy_food_csv",
    "fndds": "survey_food_csv",
    "branded": "branded_food_csv",
}
ALIASES = {"survey": "fndds"}   # accept the old name too

# branded is ~2M foods (~428 MB zip, ~2.9 GB extracted) - opt in
# explicitly; it far exceeds the Supabase free-tier database size.
DEFAULT_DATASETS = ["foundation", "sr_legacy", "fndds"]

# matches e.g. ..._foundation_food_csv_2026-04-30.zip and the odd
# ' 2019-04-02.zip' (leading space, url-encoded) historical form.
URL_RE = re.compile(
    r'/fdc-datasets/FoodData_Central_[A-Za-z_]+_csv_%?\s?\d{4}-\d{2}(?:-\d{2})?\.zip')
DATE_RE = re.compile(r'(\d{4}-\d{2}(?:-\d{2})?)\.zip$')


def fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ont-fdc-downloader"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def latest_url(html: str, marker: str) -> str | None:
    """Newest CSV zip URL for a dataset (by the date in the filename)."""
    paths = {m.group(0) for m in URL_RE.finditer(html) if marker in m.group(0)}
    if not paths:
        return None
    def key(p: str) -> str:
        m = DATE_RE.search(p)
        return m.group(1) if m else ""
    best = max(paths, key=key)
    return BASE + best.replace(" ", "").replace("%", "")


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ont-fdc-downloader"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(dest, "wb") as fh:
            while chunk := resp.read(1 << 20):     # 1 MiB
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    print(f"  {done >> 20:,} / {total >> 20:,} MiB ({pct}%)",
                          end="\r", flush=True)
    print()


def extract_flat(zip_path: Path, dest_dir: Path) -> int:
    """Extract all .csv files into dest_dir (flattened, no nested dirs)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = Path(info.filename).name
            if not name.lower().endswith(".csv") or info.is_dir():
                continue
            with zf.open(info) as src, open(dest_dir / name, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download + extract latest FDC CSV datasets")
    ap.add_argument("--datasets", nargs="+",
                    choices=list(DATASETS) + list(ALIASES),
                    default=DEFAULT_DATASETS,
                    help="which datasets (default: foundation sr_legacy "
                         "fndds; add 'branded' for the ~2M-food branded set). "
                         "'survey' aliases 'fndds'")
    ap.add_argument("--output", type=Path, default=Path("source"),
                    help="target folder (default: ./source)")
    ap.add_argument("--keep-zips", action="store_true",
                    help="keep the downloaded .zip files")
    args = ap.parse_args()

    print(f"fetching dataset list from {DOWNLOAD_PAGE} ...")
    try:
        html = fetch_page(DOWNLOAD_PAGE)
    except Exception as e:
        sys.exit(f"error: could not load the FDC download page: {e}")

    # normalize aliases (survey -> fndds) and de-duplicate
    keys = list(dict.fromkeys(ALIASES.get(k, k) for k in args.datasets))

    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fdc_dl_") as tmp:
        for key in keys:
            url = latest_url(html, DATASETS[key])
            if not url:
                print(f"warning: no CSV download found for {key}", file=sys.stderr)
                continue
            date = DATE_RE.search(url)
            print(f"\n{key}: {url.split('/')[-1]}"
                  f"{'  (' + date.group(1) + ')' if date else ''}")
            zip_path = (args.output if args.keep_zips else Path(tmp)) / f"{key}.zip"
            download(url, zip_path)
            dest = args.output / key
            count = extract_flat(zip_path, dest)
            print(f"  extracted {count} CSV files -> {dest}")

    print(f"\ndone -> {args.output}")


if __name__ == "__main__":
    main()
