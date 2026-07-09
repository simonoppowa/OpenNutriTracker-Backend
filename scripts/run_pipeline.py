#!/usr/bin/env python3
"""One-command pipeline: download -> convert -> create schema -> import
food data into Supabase, for any combination of sources and actions.

It orchestrates the existing per-source scripts (download_*, *_to_ont_csv,
shared/import_fdc.py) and sql/schema.sql - nothing is duplicated here.

Sources (--source, repeatable; default: fdc bls):
    fdc   USDA FoodData Central (foundation + sr_legacy + fndds; add
          --branded for the ~2M-food branded set)
    bls   Bundeslebensmittelschluessel 4.0 (CC BY 4.0)

Actions (--action, repeatable; default: all):
    download   fetch + extract raw data into <work>/source
    convert    raw data -> Supabase CSVs in <work>/out/<source>
    schema     run sql/schema.sql (create tables, view, RLS, indexes)
    import     bulk-load the CSVs (first source truncates, rest append)
    all        download + convert + schema + import

Examples:
    # everything, both sources:
    python run_pipeline.py --db "$SUPABASE_DB_URL"

    # only download + convert FDC (incl. branded), no DB needed:
    python run_pipeline.py --source fdc --branded --action download convert

    # data already downloaded+converted; just (re)build the DB:
    python run_pipeline.py --action schema import --db "$SUPABASE_DB_URL"

    # BLS only, append to an existing FDC-loaded DB:
    python run_pipeline.py --source bls --action convert import \
        --db "$SUPABASE_DB_URL"

Notes
-----
* Import order matters: FDC ships the shared canonical `nutrient` table,
  so when both are selected FDC is always imported before BLS. The first
  import in a run truncates; later ones append.
* schema + import need a connection string (--db or $SUPABASE_DB_URL),
  session pooler (port 5432). Requires: pip install psycopg2-binary
  (and openpyxl for BLS convert).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # scripts/
REPO = ROOT.parent
SCHEMA_SQL = REPO / "sql" / "schema.sql"

# canonical source order (FDC before BLS: BLS needs FDC's nutrient table)
SOURCE_ORDER = ["fdc", "bls"]
ALL_ACTIONS = ["download", "convert", "schema", "import"]


def run(cmd: list[str], dry: bool, env: dict | None = None) -> None:
    printable = " ".join(str(c) for c in cmd)
    print(f"\n$ {printable}")
    if dry:
        return
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        sys.exit(f"error: step failed ({r.returncode}): {printable}")


def py(script: Path, *args) -> list[str]:
    return [sys.executable, str(script), *map(str, args)]


def do_download(src: str, source_dir: Path, datasets: list[str] | None,
                dry: bool) -> None:
    if src == "fdc":
        cmd = py(ROOT / "fdc" / "download_fdc.py", "--output", source_dir)
        if datasets:
            cmd += ["--datasets", *datasets]
        run(cmd, dry)
    elif src == "bls":
        run(py(ROOT / "bls" / "download_bls.py", "--output", source_dir), dry)


def do_convert(src: str, source_dir: Path, out_dir: Path, dry: bool) -> None:
    if src == "fdc":
        run(py(ROOT / "fdc" / "fdc_to_ont_csv.py",
               "--source-dir", source_dir, "--output", out_dir), dry)
    elif src == "bls":
        run(py(ROOT / "bls" / "bls_to_ont_csv.py",
               "--source-dir", source_dir / "bls", "--output", out_dir), dry)


def do_schema(db: str, dry: bool) -> None:
    print(f"\n# create schema: {SCHEMA_SQL}")
    if dry:
        return
    sys.path.insert(0, str(ROOT / "shared"))
    import psycopg2                              # noqa: E402
    from import_fdc import normalize_db_url      # noqa: E402
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    conn = psycopg2.connect(normalize_db_url(db))
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("schema created")
    finally:
        conn.close()


def do_import(out_dir: Path, db: str, append: bool, dry: bool) -> None:
    # pass the connection string via the environment, never on the command
    # line - argv is visible in `ps` and the printed command would leak the
    # password. import_fdc.py reads $SUPABASE_DB_URL by default.
    cmd = py(ROOT / "shared" / "import_fdc.py", "--csv-dir", out_dir)
    if append:
        cmd.append("--append")
    run(cmd, dry, env={**os.environ, "SUPABASE_DB_URL": db})


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download/convert/create/import food data in one command",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", nargs="+", choices=SOURCE_ORDER,
                    default=SOURCE_ORDER, help="data sources (default: fdc bls)")
    ap.add_argument("--action", nargs="+", choices=ALL_ACTIONS + ["all"],
                    default=["all"], help="steps to run (default: all)")
    ap.add_argument("--db", default=os.environ.get("SUPABASE_DB_URL"),
                    help="Postgres connection string (default: $SUPABASE_DB_URL)")
    ap.add_argument("--work-dir", type=Path, default=Path("."),
                    help="base dir for source/ and out/ (default: .)")
    ap.add_argument("--branded", action="store_true",
                    help="include the FDC branded dataset (~2M foods)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the steps without running them")
    args = ap.parse_args()

    actions = ALL_ACTIONS if "all" in args.action else args.action
    # keep canonical order regardless of how they were passed
    sources = [s for s in SOURCE_ORDER if s in args.source]
    actions = [a for a in ALL_ACTIONS if a in actions]

    if ("schema" in actions or "import" in actions) and not args.db \
            and not args.dry_run:
        sys.exit("error: schema/import need a connection string "
                 "(--db or $SUPABASE_DB_URL)")

    source_dir = args.work_dir / "source"
    out_dirs = {s: args.work_dir / "out" / s for s in sources}
    fdc_datasets = None
    if args.branded:
        fdc_datasets = ["foundation", "sr_legacy", "fndds", "branded"]

    print(f"sources: {', '.join(sources)}   actions: {', '.join(actions)}")

    if "download" in actions:
        for s in sources:
            do_download(s, source_dir, fdc_datasets if s == "fdc" else None,
                        args.dry_run)
    if "convert" in actions:
        for s in sources:
            do_convert(s, source_dir, out_dirs[s], args.dry_run)
    if "schema" in actions:
        do_schema(args.db, args.dry_run)
    if "import" in actions:
        for i, s in enumerate(sources):
            # first import truncates (fresh load); the rest append so the
            # shared tables (food, nutrient...) are not wiped between sources
            do_import(out_dirs[s], args.db, append=(i > 0), dry=args.dry_run)

    print("\ndone." + (" (dry run)" if args.dry_run else ""))
    if "import" in actions and not args.dry_run:
        print("Reminder: food_summary is refreshed by import_fdc.py.")


if __name__ == "__main__":
    main()
