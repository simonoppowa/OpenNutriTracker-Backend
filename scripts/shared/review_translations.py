#!/usr/bin/env python3
"""Take one locale's translations from machine output to `source='verified'`,
via a CSV a native speaker can edit without touching SQL.

Covers both translated vocabularies:

    --target portions  food_portion_translation.portion_description
                       ("1 cup", "1 slice") -- what a user actually reads
    --target units     measure_unit_translation.name
                       ("cup", "slice") -- reaches 0.5% of portions, since
                       measure_unit is 'undetermined' almost everywhere

Both tables are seeded machine-translated
(source='machine', ai_generated=true). The app is expected to display only
rows a human has promoted to 'verified'; everything else falls back to the
app's own word for "serving". So a locale stays invisible until somebody
who speaks it has been through the list — around 110 rows either way,
one sitting.

Two steps, and the second refuses to run unless the first was finished.

    export SUPABASE_DB_URL='postgresql://...'

    python review_translations.py --target portions --export --locale de
    # ... a German speaker edits review/portions_de.csv ...
    python review_translations.py --target portions --apply  --locale de

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

# Portion descriptions carry the count, so they need their own keys — the
# reviewer sees "1 cup", not "cup".
AMBIGUOUS_PORTIONS = {
    "1 slice": "bread (de: Scheibe) or cake (de: Stück)",
    "1 cup": "the 240 ml measure, or a drinking vessel",
    "1 large": "a bare adjective; gender and ending depend on the food",
    "1 small": "a bare adjective; gender and ending depend on the food",
    "1 medium": "a bare adjective; gender and ending depend on the food",
    "1 thick": "a bare adjective; gender and ending depend on the food",
    "1 thin": "a bare adjective; gender and ending depend on the food",
    "1 regular": "'regular' as a size, not as 'ordinary'",
    "1 whole": "a bare adjective, as above",
    "1 submarine": "a sandwich shape with no common word in most locales",
    "1 large/king size": "a US retail size with no local equivalent",
    "1 sharing/movie theater size": "a US retail size with no local equivalent",
    "1 fun/snack size": "a US retail size with no local equivalent",
    "1 miniature/slider": "'slider' is a US term",
    "1 100 calorie package": "a US retail format",
    "1 item, any size": "generic count",
    "1 piece": "generic count",
    "1 piece/slice": "generic count",
    "1 spear": "asparagus or broccoli",
    "1 stick": "butter, celery or a snack bar",
    "1 bar": "a snack bar, not a counter",
}

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

AMBIGUOUS.update(AMBIGUOUS_PORTIONS)



class Target:
    """The two translated vocabularies, which differ only in identifiers.

    Kept as data rather than as two scripts: the rule that matters — a
    locale is promoted only when the whole list was reviewed — is the same
    for both, and duplicating it would give it two places to drift.
    """

    def __init__(self, key, table, text_col, join_table, join_key, join_text, label):
        self.key, self.table, self.text_col = key, table, text_col
        self.join_table, self.join_key, self.join_text = join_table, join_key, join_text
        self.label = label


TARGETS = {
    "portions": Target(
        key="portions",
        table="food_portion_translation",
        text_col="portion_description",
        join_table="food_portion",
        join_key="food_portion_id",
        join_text="portion_description",
        label="portion description",
    ),
    "units": Target(
        key="units",
        table="measure_unit_translation",
        text_col="name",
        join_table="measure_unit",
        join_key="measure_unit_id",
        join_text="name",
        label="measure unit",
    ),
}


def connect(db_url: str):
    if not db_url:
        sys.exit("no database URL: pass --db or set SUPABASE_DB_URL")
    return psycopg2.connect(db_url)


def fetch(conn, tgt, locale: str) -> list[tuple[str, str, str]]:
    """(english, translated, source) per DISTINCT english string.

    Distinct matters for portions: "1 cup" is 3,056 rows, and a reviewer
    must see it once. `min(source)` collapses a set that should be uniform
    anyway, and sorts 'machine' before 'verified' so a partly-promoted
    string reports the weaker state rather than the flattering one.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            select src.{tgt.join_text}, min(t.{tgt.text_col}), min(t.source)
            from {tgt.table} t
            join {tgt.join_table} src on src.id = t.{tgt.join_key}
            where t.locale = %s
            group by src.{tgt.join_text}
            order by src.{tgt.join_text}
            """,
            (locale,),
        )
        return cur.fetchall()


def export(conn, tgt, locale: str, out_dir: Path) -> Path:
    rows = fetch(conn, tgt, locale)
    if not rows:
        sys.exit(
            f"no {tgt.label} rows for locale {locale!r}. Has the seed "
            f"migration been applied?"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{tgt.key}_{locale}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for english, current, _source in rows:
            writer.writerow({
                "unit_en": english,
                "current": current,
                "corrected": "",
                "note": AMBIGUOUS.get(english, ""),
            })

    verified = sum(1 for *_, src in rows if src == "verified")
    noted = sum(1 for e, *_ in rows if e in AMBIGUOUS)
    print(f"wrote {path} — {len(rows)} {tgt.label}s, {verified} already verified")
    if noted:
        print(f"{noted} carry a note; start there")
    return path


class IncompleteReview(Exception):
    """The CSV does not account for every string the locale has."""


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
            f"{len(absent)} of {len(in_db)} entries are missing from the CSV, "
            f"so it cannot be a complete review.\nmissing: "
            + ", ".join(absent[:12]) + (" ..." if len(absent) > 12 else "")
        )
    unknown = sorted(set(reviewed) - in_db)
    if unknown:
        raise IncompleteReview(f"CSV names entries not in the database: {unknown}")

    corrections = {u: v for u, v in reviewed.items() if v and v != DELETE_MARKER}
    deletions = sorted(u for u, v in reviewed.items() if v == DELETE_MARKER)
    return corrections, deletions


def apply(conn, tgt, locale: str, path: Path, dry_run: bool, assume_yes: bool) -> None:
    if not path.exists():
        sys.exit(f"{path} not found — run --export --locale {locale} first")

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing_cols = set(FIELDS) - set(reader.fieldnames or [])
        if missing_cols:
            sys.exit(f"{path} is missing columns: {sorted(missing_cols)}")
        reviewed = {r["unit_en"]: (r["corrected"] or "").strip() for r in reader}

    in_db = {english for english, _, _ in fetch(conn, tgt, locale)}
    try:
        corrections, deletions = plan(reviewed, in_db)
    except IncompleteReview as exc:
        sys.exit(f"{path}: {exc}")

    print(f"locale {locale} ({tgt.label}): {len(in_db)} distinct entries")
    print(f"  corrections : {len(corrections)}")
    print(f"  deletions   : {len(deletions)}"
          + (f" ({', '.join(deletions[:8])})" if deletions else ""))
    print(f"  promoted to 'verified': {len(in_db) - len(deletions)}")

    if dry_run:
        print("\n--dry-run: nothing written")
        return

    if not assume_yes:
        if input(f"\napply to {locale!r} and mark it verified? [y/N] ").strip().lower() != "y":
            print("nothing written")
            return

    with conn.cursor() as cur:
        if corrections:
            # One %s only: execute_values expands exactly one placeholder
            # into the VALUES list, so the locale travels in each tuple
            # rather than as a second parameter.
            #
            # A join, not `t.<key> in (select ... where = v.english)`. The
            # subquery form is correlated, so Postgres re-scanned the whole
            # source table once per matching row — measured at 23,176 seq
            # scans and 86 seconds for *two* corrections, against 913 total
            # cost and one scan for this. Six corrections timed out.
            execute_values(
                cur,
                f"""
                update {tgt.table} t
                set {tgt.text_col} = v.translated, updated_at = now()
                from (values %s) as v(english, translated, locale),
                     {tgt.join_table} src
                where src.{tgt.join_text} = v.english
                  and t.{tgt.join_key} = src.id
                  and t.locale = v.locale
                """,
                [(en, tr, locale) for en, tr in corrections.items()],
                page_size=max(len(corrections), 1),
            )
        if deletions:
            cur.execute(
                f"""
                delete from {tgt.table} t
                using {tgt.join_table} src
                where src.id = t.{tgt.join_key}
                  and t.locale = %s
                  and src.{tgt.join_text} = any(%s)
                """,
                (locale, deletions),
            )
        cur.execute(
            f"update {tgt.table} set source = 'verified', updated_at = now() "
            f"where locale = %s",
            (locale,),
        )
    conn.commit()
    print(f"\n{locale} is verified for {tgt.label}s — the app may now show them")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", choices=sorted(TARGETS), default="portions",
                    help="which vocabulary to review (default: portions)")
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

    tgt = TARGETS[args.target]
    conn = connect(args.db)
    try:
        if args.export:
            export(conn, tgt, args.locale, args.dir)
        else:
            apply(conn, tgt, args.locale,
                  args.dir / f"{tgt.key}_{args.locale}.csv",
                  args.dry_run, args.yes)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
