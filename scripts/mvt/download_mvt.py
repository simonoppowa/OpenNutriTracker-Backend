#!/usr/bin/env python3
"""Download the Norwegian Food Composition Table (Matvaretabellen) JSON
exports into a source folder.

Matvaretabellen is published by the Norwegian Food Safety Authority
(Mattilsynet) through a static, unversioned JSON API
(https://www.matvaretabellen.no/api/), updated once a year. This fetches
the English and Norwegian Bokmaal exports mvt_to_ont_csv.py needs:

    source/mvt/
      en-foods.json          foods, English names + all constituents/portions
      nb-foods.json          same foods, Norwegian (Bokmaal) names/portions
      en-food-groups.json    food-group hierarchy (-> food_category)
      nb-food-groups.json
      en-nutrients.json      nutrient catalog (EuroFIR ids, units)
      en-sources.json        provenance codes (sourceId -> description)

Then convert:
    python mvt_to_ont_csv.py --output ../../out/mvt   # reads ./source/mvt

Usage:
    python download_mvt.py                     # -> ./source/mvt
    python download_mvt.py --output source     # also -> ./source/mvt
    python download_mvt.py --output /data      # -> /data/mvt

License: Mattilsynet publishes the data under CC BY 4.0 (its terms page,
https://www.mattilsynet.no/mat-og-drikke/matvaretabellen/opphavsrett;
data.norge.no lists the dataset under NLOD 2.0 - both allow
redistribution in a derived database). Required attribution:
    Matvaretabellen 2026. Mattilsynet. www.matvaretabellen.no

Standard library only (urllib, json).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://www.matvaretabellen.no/api"
UA = {"User-Agent": "Mozilla/5.0 (ont-mvt-downloader)"}

# (locale, endpoint, top-level key the payload must carry)
FILES = [
    ("en", "foods", "foods"),
    ("nb", "foods", "foods"),
    ("en", "food-groups", "foodGroups"),
    ("nb", "food-groups", "foodGroups"),
    ("en", "nutrients", "nutrients"),
    ("en", "sources", "sources"),
]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download the Matvaretabellen JSON exports (en + nb)")
    ap.add_argument("--output", type=Path, default=Path("source"),
                    help="base source folder; files go to <output>/mvt "
                         "(default: ./source -> ./source/mvt)")
    args = ap.parse_args()

    # always land in <output>/mvt (accepts --output source OR source/mvt)
    dest = args.output if args.output.name == "mvt" else args.output / "mvt"
    dest.mkdir(parents=True, exist_ok=True)

    for locale, endpoint, key in FILES:
        url = f"{API}/{locale}/{endpoint}.json"
        name = f"{locale}-{endpoint}.json"
        print(f"fetching {url} ...")
        try:
            body = fetch(url)
        except (urllib.error.URLError, TimeoutError) as e:
            sys.exit(f"error: could not download {url}: {e}\n"
                     f"Check your connection, or that the API still lives at "
                     f"{API}/ (documented on https://www.matvaretabellen.no/api/).")
        try:
            payload = json.loads(body)
        except ValueError as e:
            sys.exit(f"error: {url} did not return JSON ({e}) - the API may "
                     f"have moved; see https://www.matvaretabellen.no/api/")
        if not isinstance(payload, dict) or not payload.get(key):
            sys.exit(f"error: {url} has no '{key}' list - the export format "
                     f"changed; update download_mvt.py / mvt_to_ont_csv.py")
        tmp = dest / (name + ".part")
        tmp.write_bytes(body)
        os.replace(tmp, dest / name)            # never leave a half file
        print(f"  {name:22s} {len(payload[key]):>6,} {key} "
              f"({len(body) >> 10:,} KiB)")

    print(f"\ndone -> {dest}")
    print("Remember to attribute: \"Matvaretabellen 2026. Mattilsynet. "
          "www.matvaretabellen.no\" (CC BY 4.0) when using this data.")


if __name__ == "__main__":
    main()
