#!/usr/bin/env python3
"""Download the Swedish Food Agency's food composition database
(Livsmedelsverket, "Livsmedelsdatabasen") into a source folder.

Livsmedelsverket publishes the database as open data (CC BY 4.0) ONLY
through a REST API:

    https://dataportal.livsmedelsverket.se/livsmedel/swagger/index.html

The "Sök näringsinnehåll" web search also offers an Excel export, but it
is not the open-data channel: it is Swedish-only (no English names) and
its page states "När uppgifterna används, får data inte förändras" (the
data may not be altered) - so it is deliberately NOT used here. This
script pages through it politely (sequential requests, a small
delay between them, retries with back-off) and caches the raw JSON:

    source/slv/
      api_info.json                  API name / version / release date
      livsmedel_en.json              all foods, English names   (sprak=2)
      livsmedel_sv.json              all foods, Swedish names   (sprak=1)
      naringsvarden/<nummer>.json    per-food nutrient values   (sprak=2)
      klassificeringar/<nummer>.json per-food classifications   (sprak=2;
                                     carries the "Main group" category)

The run is resumable: per-food files already on disk are skipped, so an
interrupted download just continues (pass --refresh to fetch everything
again, e.g. after a new release). ~2.6k foods -> ~5.2k requests, about
15-25 minutes at the default delay.

Then convert:
    python slv_to_ont_csv.py --output ../../out/slv   # reads source/slv

Usage:
    python download_slv.py                      # -> ./source/slv
    python download_slv.py --output source      # also -> ./source/slv
    python download_slv.py --delay 0.25         # be even gentler
    python download_slv.py --refresh            # ignore the cache

License: Creative Commons Attribution 4.0 (CC BY 4.0). Livsmedelsverket:
"you have permission to use and distribute our open data and create
processing. The Swedish Food Agency must be stated as the source."
Attribute as:
    Source: Swedish Food Agency (Livsmedelsverket), Food composition
    database (Livsmedelsdatabasen), CC BY 4.0.

Standard library only (urllib, json).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://dataportal.livsmedelsverket.se/livsmedel/api/v1"
UA = {"User-Agent": "ont-slv-downloader (+https://github.com/simonoppowa/"
                    "OpenNutriTracker)",
      "Accept": "application/json"}
PAGE_SIZE = 500                    # foods per list page
SPRAK = {"sv": 1, "en": 2}         # API language codes
RETRIES = 4


def get_json(url: str, delay: float):
    """GET url and parse JSON, retrying transient failures with back-off.
    Sleeps `delay` seconds after every request to stay polite."""
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
            time.sleep(delay)
            return data
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500 and e.code != 429:      # 4xx: not transient
                raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                ConnectionError) as e:
            last = e
        time.sleep(max(delay, 1.0) * 2 ** attempt)
    raise RuntimeError(f"{url}: {last}")


def write_json(path: Path, data) -> None:
    """Atomic write, so an interrupted run never leaves a truncated cache
    file that a resumed run would trust."""
    tmp = path.with_suffix(".json.part")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def fetch_food_list(lang: str, delay: float) -> list[dict]:
    foods, offset, total = [], 0, None
    while total is None or offset < total:
        page = get_json(f"{API}/livsmedel?offset={offset}&limit={PAGE_SIZE}"
                        f"&sprak={SPRAK[lang]}", delay)
        total = page["_meta"]["totalRecords"]
        batch = page.get("livsmedel") or []
        if not batch:
            break
        foods.extend(batch)
        offset += len(batch)
    if total is not None and len(foods) != total:
        sys.exit(f"error: API reported {total} foods but returned "
                 f"{len(foods)} ({lang}) - retry later")
    return foods


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download the Livsmedelsverket food database via its API")
    ap.add_argument("--output", type=Path, default=Path("source"),
                    help="base source folder; files go to <output>/slv "
                         "(default: ./source -> ./source/slv)")
    ap.add_argument("--delay", type=float, default=0.1,
                    help="seconds to wait after each request (default 0.1)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download per-food files already cached")
    args = ap.parse_args()

    # always land in <output>/slv (accepts --output source OR source/slv)
    dest = args.output if args.output.name == "slv" else args.output / "slv"
    nut_dir, cls_dir = dest / "naringsvarden", dest / "klassificeringar"
    for d in (nut_dir, cls_dir):
        d.mkdir(parents=True, exist_ok=True)

    try:
        info = get_json(f"{API}/api-info", args.delay)
        write_json(dest / "api_info.json", info)
        print(f"API {info.get('apiName')} {info.get('apiVersion')} "
              f"(released {info.get('apiReleased')})")
        lists = {}
        for lang in ("en", "sv"):
            lists[lang] = fetch_food_list(lang, args.delay)
            write_json(dest / f"livsmedel_{lang}.json", lists[lang])
            print(f"  livsmedel_{lang}.json  {len(lists[lang]):,} foods")
    except (urllib.error.URLError, RuntimeError, KeyError) as e:
        sys.exit(f"error: could not read the food list from {API}: {e}\n"
                 f"Check your connection / the API status at "
                 f"https://dataportal.livsmedelsverket.se/livsmedel/swagger/")

    numbers = [f["nummer"] for f in lists["en"]]
    todo = [(n, kind, d) for n in numbers
            for kind, d in (("naringsvarden", nut_dir),
                            ("klassificeringar", cls_dir))
            if args.refresh or not (d / f"{n}.json").exists()]
    print(f"per-food files: {len(numbers) * 2 - len(todo):,} cached, "
          f"{len(todo):,} to fetch")
    failed = []
    for i, (n, kind, d) in enumerate(todo, 1):
        try:
            write_json(d / f"{n}.json",
                       get_json(f"{API}/livsmedel/{n}/{kind}?sprak=2",
                                args.delay))
        except (urllib.error.URLError, RuntimeError) as e:
            failed.append(f"{kind}/{n}: {e}")
        if i % 100 == 0 or i == len(todo):
            print(f"  {i:,} / {len(todo):,}", end="\r", flush=True)
    print()
    if failed:
        sys.exit(f"error: {len(failed)} request(s) failed (first: "
                 f"{failed[0]}) - re-run to fetch only the missing files")
    print(f"done -> {dest}")
    print("\nRemember to attribute the Swedish Food Agency (Livsmedelsverket), "
          "CC BY 4.0, when using this data.")


if __name__ == "__main__":
    main()
