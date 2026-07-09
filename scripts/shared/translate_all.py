#!/usr/bin/env python3
"""Universal food-description translator: DeepL-translate the English
`food.description` of EVERY source in the database into one target
language, in a single command.

Reads straight from Supabase (so it covers all imported sources at once
and needs no converted CSVs) and writes the translations into a per-source
CSV under each source's out folder:

    <out-dir>/fdc/food_translation_<locale>.csv
    <out-dir>/bls/food_translation_<locale>.csv
    <out-dir>/indb/food_translation_<locale>.csv
    <out-dir>/tbca/food_translation_<locale>.csv

Only foods that DON'T already have a translation for the target locale are
translated, so BLS German native names are skipped for --target de and
re-runs resume automatically.

When translation finishes it ASKS whether to write the results into the
`food_translation` table (source='machine', ai_generated=true, ON CONFLICT
DO NOTHING). Pass --yes to skip the prompt (CI), or answer 'n' to keep the
CSVs only and import them later with import_fdc.py --append.

Usage:
    export SUPABASE_DB_URL='postgresql://...'         # session pooler
    export DEEPL_API_KEY='xxxxxxxx-xxxx-...:fx'        # avoids argv leak

    python translate_all.py --target de                    # all sources
    python translate_all.py --target fr --source fdc indb  # some sources
    python translate_all.py --target es --limit 100 --yes  # small, auto-insert

Notes
-----
* DeepL free keys end in ':fx' (routed to api-free.deepl.com); paid keys
  go to api.deepl.com. Free tier = 500k chars/month.
* food.description is always English, so DeepL source_lang=EN.
* food_summary is locale-independent; the app joins food_translation, so no
  materialized-view refresh is needed after inserting translations.
* Requires: pip install requests psycopg2-binary
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import requests
except ImportError:
    sys.exit("error: requests missing - run: python3 -m pip install requests")
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("error: psycopg2 missing - run: python3 -m pip install psycopg2-binary")

from import_fdc import normalize_db_url          # noqa: E402  (same dir)

BATCH_SIZE = 50            # DeepL maximum texts per request
RETRIES = 3
FIELDS = ["food_id", "locale", "description", "source", "ai_generated"]
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
        if resp.status_code == 429 or resp.status_code >= 500:
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


def fetch_todo(conn, locale: str, sources: list[str] | None, limit: int | None):
    """(family, food_id, description) for foods lacking a `locale` row."""
    where = ["not exists (select 1 from food_translation t "
             "where t.food_id = f.id and t.locale = %s)",
             "f.description is not null and f.description <> ''"]
    params: list = [locale]
    if sources:
        where.append("split_part(f.source, '_', 1) = any(%s)")
        params.append(sources)
    sql = (f"select split_part(f.source, '_', 1) as family, f.id, f.description "
           f"from food f where {' and '.join(where)} order by f.id")
    if limit:
        sql += " limit %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def already_in_csv(path: Path, locale: str) -> set[int]:
    if not path.exists():
        return set()
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != FIELDS:
            sys.exit(f"error: {path} has unexpected columns {reader.fieldnames}")
        return {int(r["food_id"]) for r in reader if r["locale"] == locale}


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        print(f"{prompt} (non-interactive: assuming no; use --yes to insert)")
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Translate every source's food descriptions via DeepL")
    ap.add_argument("--target", required=True,
                    help="target locale, e.g. de, fr, es, it, pt")
    ap.add_argument("--db", default=os.environ.get("SUPABASE_DB_URL"),
                    help="connection string (default: $SUPABASE_DB_URL)")
    ap.add_argument("--api-key", default=os.environ.get("DEEPL_API_KEY"),
                    help="DeepL API key (default: $DEEPL_API_KEY)")
    ap.add_argument("--source", nargs="+", default=None,
                    help="limit to these source families (fdc bls indb tbca)")
    ap.add_argument("--out-dir", type=Path, default=Path("out"),
                    help="base output dir; CSVs go to <out-dir>/<source>/ "
                         "(default: ./out)")
    ap.add_argument("--yes", action="store_true",
                    help="insert into the DB without prompting")
    ap.add_argument("--limit", type=int, default=None,
                    help="translate at most N foods (for testing)")
    args = ap.parse_args()

    if not args.db:
        sys.exit("error: no connection string (--db or $SUPABASE_DB_URL)")
    if not args.api_key:
        sys.exit("error: no DeepL key (--api-key or $DEEPL_API_KEY)")

    locale = args.target.lower()
    deepl_target = DEEPL_TARGET.get(locale, locale.upper())
    url = deepl_url(args.api_key)

    conn = psycopg2.connect(normalize_db_url(args.db))
    conn.autocommit = False
    try:
        rows = fetch_todo(conn, locale, args.source, args.limit)

        # group by source family, dropping ids already written to that CSV
        by_family: dict[str, list[tuple[int, str]]] = {}
        csv_paths: dict[str, Path] = {}
        for family, fid, desc in rows:
            path = args.out_dir / family / f"food_translation_{locale}.csv"
            csv_paths[family] = path
            by_family.setdefault(family, []).append((fid, desc))
        for family in list(by_family):
            done = already_in_csv(csv_paths[family], locale)
            by_family[family] = [(i, d) for i, d in by_family[family]
                                 if i not in done]
            if not by_family[family]:
                del by_family[family]

        total = sum(len(v) for v in by_family.values())
        if not total:
            print(f"nothing to translate for '{locale}'")
            return
        chars = sum(len(d) for v in by_family.values() for _, d in v)
        print(f"translating {total:,} foods (~{chars:,} chars) to "
              f"{deepl_target} across {len(by_family)} source(s): "
              f"{', '.join(sorted(by_family))}")

        # ---- translate + write per-source CSVs -------------------------
        done_total = 0
        for family, items in sorted(by_family.items()):
            path = csv_paths[family]
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not path.exists()
            with open(path, "a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if new_file:
                    w.writerow(FIELDS)
                for i in range(0, len(items), BATCH_SIZE):
                    batch = items[i:i + BATCH_SIZE]
                    texts = translate_batch(
                        url, args.api_key, [d for _, d in batch], deepl_target)
                    if len(texts) != len(batch):
                        sys.exit("error: DeepL returned a different count")
                    w.writerows((fid, locale, txt, "machine", "true")
                                for (fid, _), txt in zip(batch, texts))
                    fh.flush()
                    done_total += len(batch)
                    print(f"  {done_total:,}/{total:,}", end="\r", flush=True)
            print(f"\n  {family}: -> {path}")

        # ---- offer to write into the database --------------------------
        if args.yes or confirm(
                f"\nInsert {total:,} '{locale}' translations into "
                f"food_translation?"):
            inserted = 0
            for path in csv_paths.values():
                with open(path, newline="", encoding="utf-8") as fh:
                    reader = csv.DictReader(fh)
                    data = [(int(r["food_id"]), r["locale"], r["description"],
                             r["source"], r["ai_generated"] == "true")
                            for r in reader if r["locale"] == locale]
                for j in range(0, len(data), 500):
                    with conn.cursor() as cur:
                        psycopg2.extras.execute_values(cur,
                            "insert into food_translation (food_id, locale, "
                            "description, source, ai_generated) values %s "
                            "on conflict (food_id, locale) do nothing",
                            data[j:j + 500])
                    conn.commit()
                    inserted += len(data[j:j + 500])
            print(f"inserted/kept {inserted:,} rows in food_translation "
                  f"(existing rows left untouched).")
        else:
            print(f"kept CSVs only. Import later with:\n"
                  f"  python import_fdc.py --csv-dir {args.out_dir}/<source> --append")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
