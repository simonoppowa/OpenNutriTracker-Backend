#!/usr/bin/env python3
"""Load the converted Food Data Central CSVs into Supabase.

Python replacement for import_fdc.sql - no psql needed, just the
database connection string.

Usage:
    python import_fdc.py \
        --db "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres" \
        --csv-dir <dir with the converted CSVs> \
        [--append]

    # the connection string can also come from the environment:
    export SUPABASE_DB_URL='postgresql://...'
    python import_fdc.py --csv-dir fdc_out/

Notes
-----
* Use the SESSION pooler string (port 5432) or direct connection -
  not the transaction pooler (6543); COPY needs a session.
* By default existing rows in the food tables are TRUNCATED first
  (idempotent re-imports). Pass --append to skip that (e.g. when the
  tables are empty or you loaded BLS/INDB first - though FDC should
  normally be loaded before the other sources).
* Tables must already exist: run schema.sql once beforehand.
* Requires: pip install psycopg2-binary

The same script works for the BLS / INDB CSV folders too, with
--append (their converters emit a subset of the same tables):
    python import_fdc.py --csv-dir bls_out/ --append
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

try:
    import psycopg2
except ImportError:
    sys.exit("error: psycopg2 missing - run: pip install psycopg2-binary")

# FK-safe load order; missing/empty files are skipped.
TABLES = [
    "food_source",
    "nutrient",
    "nutrient_mapping",
    "food_category",
    "food",
    "food_translation",
    "food_nutrient",
    "measure_unit",
    "food_portion",
    "market_acquisition",
    "input_food",
    "food_component",
    "retention_factor",
    "bls_component",
    "food_image",
    "food_alias",
]

# Column lists are derived from each CSV's header row, so files may
# omit defaulted columns (food_translation.updated_at, food_alias.id...).

# Identity columns to realign after importing explicit ids.
IDENTITY_FIX = ["food_image", "food_alias"]


def normalize_db_url(url: str) -> str:
    """Percent-encode the password inside a postgresql:// URL.

    Supabase passwords often contain @ : / # etc., which break URL
    parsing when pasted raw. This splits the URL structurally (last
    '@' separates credentials from host, first ':' separates user
    from password, so raw passwords containing both still parse) and
    re-encodes the password. Already-encoded passwords pass through
    unchanged.
    """
    scheme_sep = "://"
    if scheme_sep not in url or "@" not in url:
        return url
    scheme, rest = url.split(scheme_sep, 1)
    creds, host = rest.rsplit("@", 1)
    if ":" not in creds:
        return url
    user, password = creds.split(":", 1)
    if quote(unquote(password), safe="") != password:   # not yet encoded
        password = quote(password, safe="")
    return f"{scheme}{scheme_sep}{user}:{password}@{host}"


def has_data(path: Path) -> bool:
    """True if the CSV has at least one row besides the header."""
    with open(path, encoding="utf-8") as fh:
        fh.readline()
        return bool(fh.readline())


def column_list(path: Path) -> str:
    """Build the COPY column list from the CSV header row."""
    import csv as _csv
    with open(path, newline="", encoding="utf-8-sig") as fh:
        header = next(_csv.reader(fh))
    cols = [c.strip() for c in header]
    if not all(c.replace("_", "").isalnum() for c in cols):
        sys.exit(f"error: suspicious column names in {path}: {cols}")
    return "(" + ", ".join(cols) + ")"


def main() -> None:
    ap = argparse.ArgumentParser(description="Import converted food CSVs into Supabase")
    ap.add_argument("--db", default=os.environ.get("SUPABASE_DB_URL"),
                    help="connection string (default: $SUPABASE_DB_URL)")
    ap.add_argument("--csv-dir", required=True, type=Path)
    ap.add_argument("--append", action="store_true",
                    help="skip the initial TRUNCATE (append to existing data)")
    args = ap.parse_args()

    if not args.db:
        sys.exit("error: no connection string (--db or $SUPABASE_DB_URL)")
    args.db = normalize_db_url(args.db)
    if not args.csv_dir.is_dir():
        sys.exit(f"error: {args.csv_dir} is not a directory")
    if ":6543" in args.db:
        sys.exit("error: transaction pooler (port 6543) does not support "
                 "COPY - use the session pooler string (port 5432)")

    present = [t for t in TABLES if (args.csv_dir / f"{t}.csv").exists()]
    if not present:
        sys.exit(f"error: no known table CSVs found in {args.csv_dir}")

    print(f"connecting ...")
    conn = psycopg2.connect(args.db)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            if not args.append:
                targets = ", ".join(present)
                print(f"truncate {targets}")
                cur.execute(f"truncate {targets} restart identity cascade")

            for t in present:
                path = args.csv_dir / f"{t}.csv"
                if not has_data(path):
                    print(f"skip  {t} (empty)")
                    continue
                cols = column_list(path)
                started = time.time()
                with open(path, encoding="utf-8") as fh:
                    cur.copy_expert(
                        f"copy {t} {cols} from stdin with (format csv, header true)",
                        fh)
                print(f"load  {t:20s} {cur.rowcount:>9,} rows "
                      f"({time.time() - started:.1f}s)")

            for t in IDENTITY_FIX:
                if t in present:
                    cur.execute(
                        f"select setval(pg_get_serial_sequence('{t}', 'id'), "
                        f"coalesce(max(id), 1)) from {t}")
        conn.commit()
        print("committed")

        conn.autocommit = True
        with conn.cursor() as cur:
            print("refreshing food_summary ...")
            cur.execute("refresh materialized view food_summary")
            cur.execute("analyze")
            cur.execute("""
                select source, count(*),
                       count(*) filter (where energy_kcal_100 is not null)
                from food_summary group by source order by source""")
            print("\nsource            foods   with kcal")
            for source, n, kcal in cur.fetchall():
                print(f"{source:16s} {n:>7,}   {kcal:>7,}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
