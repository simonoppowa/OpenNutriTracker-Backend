#!/usr/bin/env python3
"""Machine-translate food descriptions into a target language with DeepL
and write them to a CSV (food_translation layout) - no database needed.

Input are the converted food.csv files (output of the FDC / BLS / INDB /
TBCA converters): pass the files or the folders containing them.

Each batch is appended to the output CSV and flushed immediately, and
food_ids already present there are skipped - an aborted run resumes
where it stopped.

Usage:
    python translate_foods.py \
        --api-key "xxxxxxxx-xxxx-...:fx" \
        --target de \
        --input fdc_out/ --input indb_out/ --input tbca_out/ \
        [--skip-source bls] \
        [--out food_translation_de.csv] \
        [--limit 100]      # start small to check quality/quota

Tip: when translating to German, don't pass bls_out/ (or use
--skip-source bls) - BLS foods already have native German names in
food_translation and translating them again wastes quota.

Import the CSV afterwards, e.g.:
    psql "$SUPABASE_DB_URL" -c "\\copy food_translation (food_id, locale, description, source, ai_generated) from 'food_translation_de.csv' with (format csv, header true)"
    # or: python import_fdc.py --csv-dir <dir> --append

Notes
-----
* DeepL free keys end in ':fx' and are routed to api-free.deepl.com
  automatically; paid keys go to api.deepl.com.
* ~13k FDC food names ~ 500k characters: fits inside the free tier
  (500k chars/month). Check https://www.deepl.com/pro-api
* Requires: pip install requests
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("error: requests missing - run: python3 -m pip install requests")

BATCH_SIZE = 50            # DeepL maximum texts per request
RETRIES = 3
FIELDS = ["food_id", "locale", "description", "source", "ai_generated"]

# DeepL target codes for common locales (rest passed through uppercased)
DEEPL_TARGET = {"en": "EN-US", "pt": "PT-PT", "zh": "ZH-HANS"}


def deepl_url(api_key: str) -> str:
    host = "api-free.deepl.com" if api_key.endswith(":fx") else "api.deepl.com"
    return f"https://{host}/v2/translate"


def translate_batch(url: str, api_key: str, texts: list[str],
                    target: str) -> list[str]:
    for attempt in range(1, RETRIES + 1):
        resp = requests.post(
            url,
            headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
            json={"text": texts, "source_lang": "EN", "target_lang": target},
            timeout=60,
        )
        if resp.status_code == 429 or resp.status_code >= 500:   # back off
            wait = 5 * attempt
            print(f"  deepl {resp.status_code}, retrying in {wait}s ...")
            time.sleep(wait)
            continue
        if resp.status_code == 456:
            sys.exit("error: DeepL quota exceeded for this billing period")
        if resp.status_code == 403:
            sys.exit("error: DeepL rejected the API key (403)")
        resp.raise_for_status()
        return [t["text"] for t in resp.json()["translations"]]
    sys.exit(f"error: DeepL kept failing after {RETRIES} attempts")


def load_foods(inputs: list[Path], skip_sources: set[str]) -> list[tuple[int, str]]:
    """Read (food_id, description) from converted food.csv files."""
    foods: dict[int, str] = {}
    for p in inputs:
        path = p / "food.csv" if p.is_dir() else p
        if not path.exists():
            sys.exit(f"error: {path} not found")
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            required = {"id", "source", "description"}
            if not required.issubset(reader.fieldnames or []):
                sys.exit(f"error: {path} lacks columns {required} - "
                         f"is this a converted food.csv?")
            n = 0
            for r in reader:
                if r["source"] in skip_sources or not r["description"].strip():
                    continue
                foods[int(r["id"])] = r["description"].strip()
                n += 1
        print(f"loaded {n:,} foods from {path}")
    return sorted(foods.items())


def already_translated(out_path: Path, locale: str) -> set[int]:
    """food_ids already present in the output CSV (resume support)."""
    if not out_path.exists():
        return set()
    with open(out_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != FIELDS:
            sys.exit(f"error: {out_path} exists but has unexpected columns "
                     f"{reader.fieldnames} - refusing to append")
        return {int(r["food_id"]) for r in reader if r["locale"] == locale}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Translate food descriptions from converted food.csv "
                    "files via DeepL into a CSV")
    ap.add_argument("--api-key", required=True, help="DeepL API key")
    ap.add_argument("--target", required=True,
                    help="target locale, e.g. de, fr, es, it, pt")
    ap.add_argument("--input", action="append", required=True, type=Path,
                    help="converted food.csv or its folder (repeatable)")
    ap.add_argument("--skip-source", action="append", default=[],
                    help="skip foods of this source (e.g. bls for --target de)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output CSV (default: food_translation_<target>.csv)")
    ap.add_argument("--limit", type=int, default=None,
                    help="translate at most N foods (for testing)")
    args = ap.parse_args()

    locale = args.target.lower()
    deepl_target = DEEPL_TARGET.get(locale, locale.upper())
    url = deepl_url(args.api_key)
    out_path = args.out or Path(f"food_translation_{locale}.csv")

    todo = load_foods(args.input, set(args.skip_source))
    done_ids = already_translated(out_path, locale)
    if done_ids:
        print(f"resuming: {len(done_ids):,} foods already in {out_path}")
        todo = [(fid, d) for fid, d in todo if fid not in done_ids]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print(f"nothing to do for locale '{locale}'")
        return

    total_chars = sum(len(d) for _, d in todo)
    print(f"translating {len(todo):,} foods (~{total_chars:,} chars) "
          f"to {deepl_target} -> {out_path}")

    new_file = not out_path.exists()
    done = 0
    with open(out_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(FIELDS)
        for i in range(0, len(todo), BATCH_SIZE):
            batch = todo[i:i + BATCH_SIZE]
            translated = translate_batch(
                url, args.api_key, [d for _, d in batch], deepl_target)
            if len(translated) != len(batch):
                sys.exit("error: DeepL returned a different number of texts")
            for (fid, _), text in zip(batch, translated):
                writer.writerow([fid, locale, text, "machine", "true"])
            fh.flush()                         # resumable per batch
            done += len(batch)
            print(f"  {done:,}/{len(todo):,}", end="\r", flush=True)

    print(f"\ndone: {done:,} '{locale}' translations written to {out_path} "
          f"(source='machine'). Import with \\copy or "
          f"import_fdc.py --append; nothing was written to the database.")


if __name__ == "__main__":
    main()
