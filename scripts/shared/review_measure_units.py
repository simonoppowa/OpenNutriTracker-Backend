#!/usr/bin/env python3
"""Take one locale's measure-unit translations from machine output to
`source='verified'`, via a CSV a native speaker can edit without touching
SQL.

`measure_unit_translation` is seeded machine-translated
(source='machine', ai_generated=true). The app is expected to display only
rows a human has promoted to 'verified'; everything else falls back to the
app's own word for "serving". So a locale stays invisible until somebody
who speaks it has been through the list — 116 rows, one sitting.

Two steps, and the second refuses to run unless the first was finished.

    export SUPABASE_DB_URL='postgresql://...'

    python review_measure_units.py --export --locale de      # -> review/measure_units_de.csv
    # ... a German speaker edits that file ...
    python review_measure_units.py --apply  --locale de      # corrections + promotion

The CSV has four columns and the reviewer touches exactly one:

    unit_en   the English name, as FDC publishes it. Never edit.
    current   what the machine produced. Never edit.
    corrected LEAVE BLANK if `current` is right. Otherwise the right word.
              Write `-` to say no word fits, and the row is deleted so the
              app falls back to "serving" rather than showing a bad guess.
    note      why this row might be wrong. Filled in for the ambiguous ones.

Why the whole locale at once, rather than row by row: 'verified' is a claim
that somebody read the list, and a per-row flag lets a half-finished pass
look identical to a finished one. --apply checks every row for the locale is
present in the CSV and aborts otherwise, naming what is missing.

`ai_generated` is deliberately left true after promotion. It records where
the text came from, not whether it is trusted — the same rule
`food_translation` states in the schema.

Notes
-----
* Re-runnable. Exporting a locale that is already 'verified' includes the
  verified text, so a second pass corrects rather than starting over.
* --dry-run prints the SQL and touches nothing.
* Requires: pip install psycopg2-binary
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:  # pragma: no cover - matches the other scripts' style
    sys.exit("psycopg2 missing: pip install psycopg2-binary")

FIELDS = ["unit_en", "current", "corrected", "note"]

DELETE_MARKER = "-"

# English names whose sense the record does not pin down. Surfaced in the
# CSV so a reviewer spends their attention where the guess is weakest,
# rather than reading 116 rows at an even pace.
AMBIGUOUS = {
    "cup": "the 240 ml measure, or a drinking vessel",
    "slice": "bread (de: Scheibe) or cake (de: Stück)",
    "slices": "bread or cake, as with `slice`",
    "leg": "poultry (de: Keule) or the anatomical sense",
    "back": "a cut of poultry, not a direction",
    "skin": "poultry or fruit skin",
    "shell": "egg, nut or pastry",
    "head": "of lettuce or garlic, not an animal's",
    "chunk": "generic; may read as clumsy in your language",
    "stalk": "celery or broccoli",
    "spear": "asparagus or broccoli",
    "rack": "of ribs or lamb",
    "stick": "butter, celery or a snack bar",
    "bar": "a snack bar, not a counter",
    "link": "a single sausage",
    "links": "sausages, plural",
    "order": "a restaurant portion",
    "contents": "of a package",
    "unit": "generic count",
    "each": "generic count",
    "piece": "generic count",
    "pieces": "generic count, plural",
}


def connect(db_url: str):
    if not db_url:
        sys.exit("no database URL: pass --db or set SUPABASE_DB_URL")
    return psycopg2.connect(db_url)


def export(conn, locale: str, out_dir: Path) -> Path:
    with conn.cursor() as cur:
        cur.execute(
            """
            select mu.name, mut.name, mut.source
            from measure_unit_translation mut
            join measure_unit mu on mu.id = mut.measure_unit_id
            where mut.locale = %s
            order by mu.name
            """,
            (locale,),
        )
        rows = cur.fetchall()

    if not rows:
        sys.exit(
            f"no rows for locale {locale!r}. Has the seed migration been "
            f"applied? (sql/migrations/2026-08-30_measure_unit_translation_seed.sql)"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"measure_units_{locale}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for unit_en, current, _source in rows:
            writer.writerow(
                {
                    "unit_en": unit_en,
                    "current": current,
                    "corrected": "",
                    "note": AMBIGUOUS.get(unit_en, ""),
                }
            )

    verified = sum(1 for *_, s in rows if s == "verified")
    print(f"wrote {path} — {len(rows)} units, {verified} already verified")
    print(f"{len([r for r in rows if r[0] in AMBIGUOUS])} carry a note; start there")
    return path


class IncompleteReview(Exception):
    """The CSV does not account for every row the locale has."""


def plan(reviewed: dict[str, str], in_db: set[str]) -> tuple[dict[str, str], list[str]]:
    """What --apply would do, decided without touching a database.

    Separate from [apply] so the rule this script exists to enforce can be
    tested without a Postgres: promoting a locale claims somebody read the
    whole list, and a CSV that has quietly lost rows would make a partial
    pass indistinguishable from a finished one.
    """
    absent = sorted(in_db - set(reviewed))
    if absent:
        raise IncompleteReview(
            f"{len(absent)} of {len(in_db)} units are missing from the CSV, "
            f"so it cannot be a complete review.\nmissing: "
            + ", ".join(absent[:12]) + (" ..." if len(absent) > 12 else "")
        )
    unknown = sorted(set(reviewed) - in_db)
    if unknown:
        raise IncompleteReview(f"CSV names units not in the database: {unknown}")

    corrections = {u: v for u, v in reviewed.items() if v and v != DELETE_MARKER}
    deletions = sorted(u for u, v in reviewed.items() if v == DELETE_MARKER)
    return corrections, deletions


def apply(conn, locale: str, path: Path, dry_run: bool, assume_yes: bool) -> None:
    if not path.exists():
        sys.exit(f"{path} not found — run --export --locale {locale} first")

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing_cols = set(FIELDS) - set(reader.fieldnames or [])
        if missing_cols:
            sys.exit(f"{path} is missing columns: {sorted(missing_cols)}")
        reviewed = {r["unit_en"]: (r["corrected"] or "").strip() for r in reader}

    with conn.cursor() as cur:
        cur.execute(
            """
            select mu.name
            from measure_unit_translation mut
            join measure_unit mu on mu.id = mut.measure_unit_id
            where mut.locale = %s
            """,
            (locale,),
        )
        in_db = {r[0] for r in cur.fetchall()}

    try:
        corrections, deletions = plan(reviewed, in_db)
    except IncompleteReview as exc:
        sys.exit(f"{path}: {exc}")

    print(f"locale {locale}: {len(in_db)} units")
    print(f"  corrections : {len(corrections)}")
    print(f"  deletions   : {len(deletions)}"
          + (f" ({', '.join(deletions[:8])})" if deletions else ""))
    print(f"  promoted to 'verified': {len(in_db) - len(deletions)}")

    if dry_run:
        print("\n--dry-run: nothing written")
        return

    if not assume_yes:
        answer = input(f"\napply to {locale!r} and mark it verified? [y/N] ")
        if answer.strip().lower() != "y":
            print("nothing written")
            return

    with conn.cursor() as cur:
        if corrections:
            # One %s only: execute_values expands exactly one placeholder
            # into the VALUES list, so the locale travels in each tuple
            # rather than as a second parameter.
            execute_values(
                cur,
                """
                update measure_unit_translation mut
                set name = v.name, updated_at = now()
                from (values %s) as v(unit, name, locale)
                join measure_unit mu on mu.name = v.unit
                where mut.measure_unit_id = mu.id
                  and mut.locale = v.locale
                """,
                [(unit, name, locale) for unit, name in corrections.items()],
                page_size=max(len(corrections), 1),
            )
        if deletions:
            cur.execute(
                """
                delete from measure_unit_translation mut
                using measure_unit mu
                where mu.id = mut.measure_unit_id
                  and mut.locale = %s
                  and mu.name = any(%s)
                """,
                (locale, deletions),
            )
        cur.execute(
            """
            update measure_unit_translation
            set source = 'verified', updated_at = now()
            where locale = %s
            """,
            (locale,),
        )
    conn.commit()
    print(f"\n{locale} is verified — the app may now show these units")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--export", action="store_true",
                      help="write the review CSV for --locale")
    mode.add_argument("--apply", action="store_true",
                      help="apply the edited CSV and promote the locale")
    ap.add_argument("--locale", required=True,
                    help="BCP 47 code: de, cs, it, pl, sk, tr, uk, zh")
    ap.add_argument("--db", default=os.environ.get("SUPABASE_DB_URL"),
                    help="defaults to $SUPABASE_DB_URL")
    ap.add_argument("--dir", type=Path, default=Path("review"),
                    help="where the CSVs live (default: ./review)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --apply: print what would change, write nothing")
    ap.add_argument("--yes", action="store_true",
                    help="with --apply: skip the confirmation prompt")
    args = ap.parse_args()

    conn = connect(args.db)
    try:
        if args.export:
            export(conn, args.locale, args.dir)
        else:
            apply(conn, args.locale, args.dir / f"measure_units_{args.locale}.csv",
                  args.dry_run, args.yes)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
