#!/usr/bin/env python3
"""Build an offline SQLite food database (``foods.sqlite``) from converted CSVs.

The app normally reads the hosted Supabase database through five RPCs
(``sql/schema.sql`` section 6b plus the migrations in ``sql/migrations``).
This script reproduces what those RPCs need as one SQLite file a phone can
ship, so the app can answer the same calls locally. No Postgres is involved:
it reads the per-source CSV folders the converters write (``out/fdc``,
``out/bls``, ...) and the two ``*_translation_seed.sql`` migrations.

Usage (from scripts/):

    python3 shared/build_sqlite.py \\
        --csv-dir out/fdc out/bls out/ciqual out/fineli out/frida out/mvt out/slv \\
        --output out/sqlite/foods.sqlite

    python3 shared/build_sqlite.py --check out/sqlite/foods.sqlite   # sanity checks
    python3 shared/test_build_sqlite.py                              # fixture tests

It writes ``foods.sqlite``, ``foods.sqlite.gz`` and ``ATTRIBUTION.md`` into
the output directory. ``--csv-dir`` order matters only as it does for
``import_fdc.py``: a primary-key clash between two folders is an error here as
it would be there.

What the file holds (schema version ``SCHEMA_VERSION``)
------------------------------------------------------

* ``food_summary`` -- the materialized view, row for row: the same 40 columns
  with the same derivations (24 pivoted nutrients, default serving from the
  first portion by ``seq_num nulls last, id``, ``serving_unit`` 'undetermined'
  -> 'portion', ``serving_size`` falling back to "<amount> <modifier>",
  ``has_portion`` = ``food_has_deliverable_portion``). Two helper columns
  serve the RPCs: ``title_key`` = ``lower(btrim(split_part(name, ',', 1)))``
  for the title-first ordering, and ``default_portion_description`` (the
  first portion's raw text) for ``portion_labels_by_food_ids``.
* ``food_translation`` -- every translation row plus ``title_key``.
* ``food_portion`` -- only the portions ``portions_by_food_ids`` can return,
  with its ``row_number()`` precomputed as ``seq``.
* ``food_portion_translation`` / ``measure_unit_translation`` -- the seed
  migrations, keyed by the English text they were joined on (the seeds and
  ``review_translations.py`` both work per distinct string, so a text key
  holds exactly what the per-id Postgres rows hold). All seeded rows are
  ``source='machine'``; the RPCs read only ``'verified'`` ones.
* ``food_source`` (with ``license_note``) and ``meta`` (schema version, build
  date, per-source counts).
* FTS5: ``food_fts`` on ``food_summary.name`` with ``porter unicode61``
  (stands in for ``to_tsvector('english', ...)``) and
  ``food_translation_fts`` with ``unicode61 remove_diacritics 2`` (stands in
  for ``'simple'``). Both are external-content tables, so the text is stored
  once.

Numbers keep the type PostgREST would give them: a numeric whose text has no
decimal point is stored as an integer, anything else as a real, so the app
decodes ``125`` and ``246.0`` exactly as it decodes the JSON.

The queries the app runs are in ``OfflineRpc`` below (the app's
``SqliteFoodRpcClient`` mirrors them) -- see that class for every place the
SQLite search can differ from Postgres.

Notes
-----
* Standard library only (sqlite3, csv, gzip); needs Python 3.12+ for
  ``csv.QUOTE_NOTNULL``, which tells an unquoted empty field (NULL to COPY)
  from a quoted one (''), as Postgres's CSV COPY does.
* Machine translations from ``translate_all.py``
  (``food_translation_<locale>.csv``) are loaded after the native files with
  "on conflict do nothing", as that script inserts them.
* ``food_alias``, tags, ``input_food`` and the other import-time tables are
  not read by any RPC and are left out.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import re
import shutil
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("error: build_sqlite.py needs Python 3.12+ (csv.QUOTE_NOTNULL)")

SCHEMA_VERSION = 1

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MIGRATIONS = SCRIPT_DIR.parent.parent / "sql" / "migrations"
PORTION_SEED = "2026-08-30_food_portion_translation_seed.sql"
UNIT_SEED = "2026-08-30_measure_unit_translation_seed.sql"

# Page sizes tried when --page-size is "auto"; the smallest file wins.
PAGE_SIZES = (1024, 2048, 4096, 8192, 16384)

# food_summary's pivot: canonical nutrient id -> column (MealNutrimentsDBO).
NUTRIENT_COLUMNS = [
    (1, "energy_kcal_100"), (2, "carbohydrates_100"), (3, "fat_100"),
    (4, "proteins_100"), (5, "sugars_100"), (6, "saturated_fat_100"),
    (7, "fiber_100"), (8, "monounsaturated_fat_100"),
    (9, "polyunsaturated_fat_100"), (10, "trans_fat_100"),
    (11, "cholesterol_100"), (12, "sodium_100"), (13, "potassium_100"),
    (14, "magnesium_100"), (15, "calcium_100"), (16, "iron_100"),
    (17, "zinc_100"), (18, "phosphorus_100"), (19, "vitamin_a_100"),
    (20, "vitamin_c_100"), (21, "vitamin_d_100"), (22, "vitamin_b6_100"),
    (23, "vitamin_b12_100"), (24, "niacin_100"),
]

# The view's columns, in the view's order: what every summary RPC returns.
SUMMARY_COLUMNS = [
    "food_id", "source", "source_code", "name", "short_title", "brands",
    "barcode", "category", "serving_quantity", "serving_unit",
    "serving_size", "serving_gram_weight", "thumbnail_url", "main_image_url",
    "tags", *(c for _, c in NUTRIENT_COLUMNS), "has_portion",
]

# Licence and attribution per food_source.code, from README.md / AGENTS.md.
ATTRIBUTION = {
    "fdc": ("USDA FoodData Central", "CC0 1.0 (public domain)",
            "U.S. Department of Agriculture, Agricultural Research Service. "
            "FoodData Central. https://fdc.nal.usda.gov"),
    "fdc_branded": ("USDA FoodData Central (Branded Foods)",
                    "CC0 1.0 (public domain); label data (c) manufacturers",
                    "U.S. Department of Agriculture, Agricultural Research "
                    "Service. FoodData Central. https://fdc.nal.usda.gov"),
    "bls": ("Bundeslebensmittelschlüssel (BLS) 4.0", "CC BY 4.0",
            "Max Rubner-Institut, Bundeslebensmittelschlüssel (BLS) 4.0. "
            "https://www.blsdb.de"),
    "indb": ("Anuvaad Indian Nutrient Databank (INDB)", "CC BY 4.0",
             "Anuvaad INDB, https://anuvaad.org.in"),
    "tbca": ("Tabela Brasileira de Composição de Alimentos (TBCA)",
             "free with attribution",
             "TBCA, Universidade de São Paulo (USP) / Food Research Center "
             "(FoRC). https://www.tbca.net.br"),
    "ciqual": ("Table de composition nutritionnelle des aliments Ciqual",
               "Licence Ouverte / Etalab 2.0",
               "Anses. 2025. Table de composition nutritionnelle des aliments "
               "Ciqual. https://doi.org/10.57745/RDMHWY"),
    "fineli": ("Fineli (Finnish Food Composition Database)", "CC BY 4.0",
               "Finnish Institute for Health and Welfare (THL), Fineli. "
               "https://fineli.fi"),
    "frida": ("Danish Food Composition Database (Frida)", "CC BY 4.0",
              "The Danish Food Composition Database "
              "(https://fcdb.fooddata.dk), version 6.1, May 2026, National "
              "Food Institute, Technical University of Denmark. "
              "https://doi.org/10.11583/DTU.32312844 (its documentation PDFs "
              "are (c) DTU, not CC BY, and are not included)"),
    "mvt": ("Matvaretabellen (Norwegian Food Composition Table)",
            "CC BY 4.0 (Mattilsynet's terms; data.norge.no lists NLOD 2.0)",
            "Matvaretabellen 2026. Mattilsynet. www.matvaretabellen.no"),
    "slv": ("Livsmedelsdatabasen (Swedish Food Composition Database)",
            "CC BY 4.0",
            "Swedish Food Agency (Livsmedelsverket). "
            "https://www.livsmedelsverket.se"),
}

# Postgres's english.stop (snowball), the list websearch_to_tsquery('english')
# drops. FTS5 has no stopwords, so OfflineRpc drops them from the query.
ENGLISH_STOPWORDS = frozenset("""
i me my myself we our ours ourselves you your yours yourself yourselves he
him his himself she her hers herself it its itself they them their theirs
themselves what which who whom this that these those am is are was were be
been being have has had having do does did doing a an the and but if or
because as until while of at by for with about against between into through
during before after above below to from up down in out on off over under
again further then once here there when where why how all any both each few
more most other some such no nor not only own same so than too very s t can
will just don should now
""".split())

# food_has_deliverable_portion's regex, '\m' / '\M' being word boundaries.
_NOT_DELIVERABLE = re.compile(
    r"\bNFS\b|\bNS as to\b|\byields\b|^Guideline amount", re.IGNORECASE)
_SEED_ROW = re.compile(
    r"^\s*\('((?:[^']|'')*)',\s*'((?:[^']|'')*)',\s*'((?:[^']|'')*)'\),?\s*$")
# A query word as the FTS tables tokenize it: letters and digits, with '/'
# kept inside (tokenchars '/'), as Postgres keeps "mango/papaya" one token.
_WORD = re.compile(r"(?:[^\W_]|/)*[^\W_](?:[^\W_]|/)*")
FTS_ENGLISH = "porter unicode61 tokenchars '/'"
FTS_TRANSLATION = "unicode61 remove_diacritics 2 tokenchars '/'"


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- helpers --

def pg_numeric(text: str | None):
    """A numeric CSV field as PostgREST would hand it to the app.

    Postgres keeps a numeric's scale ('1.50' stays 1.50) and PostgREST
    writes that text as a JSON number, so '125' decodes to an int and
    '246.0' to a double. Same here: no decimal point -> int, else float.
    """
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise BuildError(f"invalid numeric {text!r}") from None
    if not d.is_finite():
        raise BuildError(f"non-finite numeric {text!r}")
    t = pg_numeric_text(s)
    return float(t) if "." in t else int(t)


def pg_numeric_text(text: str | None) -> str | None:
    """`numeric::text` -- the scale kept, exponent expanded, '-0' as '0'."""
    if text is None or not text.strip():
        return None
    d = Decimal(text.strip())
    t = format(d, "f")
    if d == 0:
        t = t.lstrip("-")
    return t


def pg_int(text: str | None) -> int | None:
    if text is None or not text.strip():
        return None
    try:
        return int(text.strip())
    except ValueError:
        raise BuildError(f"invalid integer {text!r}") from None


def pg_bool(text: str | None) -> int:
    if text is None or not text.strip():
        return 0
    v = text.strip().lower()
    if v in ("t", "true", "1", "yes", "y", "on"):
        return 1
    if v in ("f", "false", "0", "no", "n", "off"):
        return 0
    raise BuildError(f"invalid boolean {text!r}")


def title_key(text: str | None) -> str | None:
    """lower(btrim(split_part(text, ',', 1))) -- btrim strips spaces only."""
    if text is None:
        return None
    return text.split(",", 1)[0].strip(" ").lower()


def is_deliverable(description: str | None) -> int:
    """The regex half of food_has_deliverable_portion."""
    return 0 if description is None or _NOT_DELIVERABLE.search(description) \
        else 1


def serving_size(description, amount_text, modifier):
    """food_summary.serving_size: coalesce(nullif(pd, ''),
    trim(concat_ws(' ', amount::text, modifier)) when modifier <> '')."""
    if description is not None and description != "":
        return description
    if modifier is not None and modifier != "":
        parts = [p for p in (amount_text, modifier) if p is not None]
        return " ".join(parts).strip(" ")
    return None


# ------------------------------------------------------------- CSV loading --

# table -> (columns kept, converter per column, defaults for absent columns)
def _T(text: str | None) -> str | None:
    return text


_I, _N, _B = pg_int, pg_numeric, pg_bool
TABLES = {
    "food_source": {"code": _T, "name": _T, "license_note": _T},
    "food_category": {"id": _I, "source": _T, "description": _T},
    "food": {"id": _I, "source": _T, "source_code": _T, "description": _T,
             "short_title": _T, "food_category_id": _I},
    "food_nutrient": {"food_id": _I, "nutrient_id": _I, "amount": _N},
    "measure_unit": {"id": _I, "name": _T},
    "food_portion": {"id": _I, "food_id": _I, "amount": _N,
                     "measure_unit_id": _I, "portion_description": _T,
                     "modifier": _T, "seq_num": _I, "gram_weight": _N},
    "market_acquisition": {"food_id": _I, "upc_code": _T,
                           "brand_description": _T},
    "food_image": {"id": _I, "food_id": _I, "kind": _T, "storage_path": _T,
                   "external_url": _T},
    "food_translation": {"food_id": _I, "locale": _T, "description": _T,
                         "source": _T, "ai_generated": _B},
}
DEFAULTS = {"food_translation": {"source": "machine", "ai_generated": 0}}

STAGE_DDL = """
create table stage.food_source (code text primary key, name text not null,
    license_note text);
create table stage.food_category (id integer primary key, source text,
    description text not null);
create table stage.food (id integer primary key, source text not null,
    source_code text not null, description text not null, short_title text,
    food_category_id integer, unique (source, source_code));
create table stage.food_nutrient (food_id integer not null,
    nutrient_id integer not null, amount not null,
    unique (food_id, nutrient_id));
create table stage.measure_unit (id integer primary key, name text not null);
create table stage.food_portion (id integer primary key,
    food_id integer not null, amount, amount_text text,
    measure_unit_id integer not null, portion_description text,
    modifier text, seq_num integer, gram_weight not null);
create table stage.market_acquisition (food_id integer primary key,
    upc_code text, brand_description text);
create table stage.food_image (id integer primary key,
    food_id integer not null, kind text not null
        check (kind in ('thumbnail', 'main')),
    storage_path text, external_url text);
create table stage.food_translation (food_id integer not null,
    locale text not null, description text not null, source text not null
        check (source in ('native', 'machine', 'community', 'verified')),
    ai_generated integer not null, primary key (food_id, locale));
"""


def read_csv(path: Path):
    """Rows as dicts; an unquoted empty field is None (COPY's NULL)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh, quoting=csv.QUOTE_NOTNULL)
        try:
            header = [c.strip() for c in next(reader)]
        except StopIteration:
            return
        for lineno, row in enumerate(reader, start=2):
            if not row:
                continue
            yield lineno, dict(zip(header, row))


def load_table(conn, table: str, path: Path, ignore_conflicts=False) -> int:
    spec = TABLES[table]
    defaults = DEFAULTS.get(table, {})
    cols = list(spec)
    extra = ["amount_text"] if table == "food_portion" else []
    verb = "insert or ignore" if ignore_conflicts else "insert"
    sql = (f"{verb} into stage.{table} ({', '.join(cols + extra)}) "
           f"values ({', '.join('?' * (len(cols) + len(extra)))})")
    n = 0
    batch = []
    for lineno, row in read_csv(path):
        try:
            values = []
            for c in cols:
                if c in row:
                    values.append(spec[c](row[c]))
                else:
                    values.append(defaults.get(c))
            if extra:
                values.append(pg_numeric_text(row.get("amount")))
        except BuildError as e:
            raise BuildError(f"{path}:{lineno}: {e}") from None
        batch.append(values)
        if len(batch) >= 5000:
            n += _flush(conn, sql, batch, path)
    n += _flush(conn, sql, batch, path)
    return n


def _flush(conn, sql, batch, path) -> int:
    try:
        before = conn.total_changes
        conn.executemany(sql, batch)
        done = conn.total_changes - before
    except sqlite3.IntegrityError as e:
        raise BuildError(
            f"{path}: {e} -- Postgres would reject this file too; is the "
            "same source folder passed twice?") from None
    batch.clear()
    return done


def load_csv_dirs(conn, dirs: list[Path]) -> None:
    order = ["food_source", "food_category", "food", "food_nutrient",
             "measure_unit", "food_portion", "market_acquisition",
             "food_image", "food_translation"]
    for d in dirs:
        if not d.is_dir():
            raise BuildError(f"{d} is not a directory (run the converter "
                             "first, or check the --csv-dir path)")
        print(f"loading {d}")
        for table in order:
            path = d / f"{table}.csv"
            if path.exists():
                n = load_table(conn, table, path)
                print(f"  {table:22s} {n:>9,}")
    # translate_all.py output, inserted "on conflict do nothing" after the
    # native names it must never overwrite.
    for d in dirs:
        for path in sorted(d.glob("food_translation_*.csv")):
            n = load_table(conn, "food_translation", path,
                           ignore_conflicts=True)
            print(f"  {path.name:22s} {n:>9,} (machine, conflicts skipped)")


def check_references(conn) -> None:
    """The foreign keys Postgres would have enforced during the import."""
    checks = [
        ("food.source -> food_source",
         "select count(*) from stage.food f where not exists "
         "(select 1 from stage.food_source s where s.code = f.source)"),
        ("food.food_category_id -> food_category",
         "select count(*) from stage.food f where f.food_category_id is not "
         "null and not exists (select 1 from stage.food_category c "
         "where c.id = f.food_category_id)"),
        ("food_nutrient.food_id -> food",
         "select count(*) from stage.food_nutrient n where not exists "
         "(select 1 from stage.food f where f.id = n.food_id)"),
        ("food_portion.food_id -> food",
         "select count(*) from stage.food_portion p where not exists "
         "(select 1 from stage.food f where f.id = p.food_id)"),
        ("food_portion.measure_unit_id -> measure_unit (FDC's "
         "measure_unit.csv; pass out/fdc too)",
         "select count(*) from stage.food_portion p where not exists "
         "(select 1 from stage.measure_unit m where m.id = p.measure_unit_id)"),
        ("food_translation.food_id -> food",
         "select count(*) from stage.food_translation t where not exists "
         "(select 1 from stage.food f where f.id = t.food_id)"),
    ]
    bad = []
    for label, sql in checks:
        n = conn.execute(sql).fetchone()[0]
        if n:
            bad.append(f"{n:,} rows violate {label}")
    if bad:
        raise BuildError("foreign keys Postgres would enforce:\n  "
                         + "\n  ".join(bad))


def read_seed(path: Path) -> list[tuple[str, str, str]]:
    """The (english, locale, translated) VALUES rows of a seed migration."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _SEED_ROW.match(line)
        if m:
            rows.append(tuple(g.replace("''", "'") for g in m.groups()))
    if not rows:
        raise BuildError(f"{path}: no VALUES rows found")
    return rows


REVIEW_NAME = re.compile(r"(portions|units)_([A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*)\.csv")
REVIEW_TARGETS = {
    "portions": ("food_portion_translation", "portion_description", "label"),
    "units": ("measure_unit_translation", "unit", "name"),
}


def apply_reviews(conn, paths: list[Path]) -> None:
    """Promote reviewed locales to 'verified', as review_translations.py
    --apply does on the live database.

    Takes the CSVs that script exports (`portions_<locale>.csv`,
    `units_<locale>.csv`; columns unit_en, current, corrected, note) after a
    native speaker filled `corrected`: blank keeps the text, `-` deletes the
    row, anything else replaces it, and every remaining row of the locale
    becomes 'verified'. Like --apply it refuses a CSV that does not account
    for every string the locale has, so a half-finished pass cannot ship as
    verified.
    """
    for path in paths:
        m = REVIEW_NAME.fullmatch(path.name)
        if not m:
            raise BuildError(f"{path}: expected portions_<locale>.csv or "
                             "units_<locale>.csv (review_translations.py "
                             "--export names)")
        target, locale = m.groups()
        table, key, text = REVIEW_TARGETS[target]
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if not {"unit_en", "corrected"} <= set(reader.fieldnames or []):
                raise BuildError(f"{path}: needs unit_en and corrected "
                                 "columns")
            reviewed = {r["unit_en"]: (r["corrected"] or "").strip()
                        for r in reader}
        in_db = {row[0] for row in conn.execute(
            f"select {key} from {table} where locale = ?", (locale,))}
        absent = sorted(in_db - set(reviewed))
        if absent:
            raise BuildError(
                f"{path}: {len(absent)} of {len(in_db)} {target} for "
                f"{locale!r} are missing, so it cannot be a complete review: "
                + ", ".join(absent[:12]) + (" ..." if len(absent) > 12 else ""))
        unknown = sorted(set(reviewed) - in_db)
        if unknown:
            print(f"  note: {path.name}: {len(unknown)} entries not in this "
                  "build's data, ignored")
        for english, corrected in reviewed.items():
            if english not in in_db or not corrected:
                continue
            if corrected == "-":
                conn.execute(f"delete from {table} where {key} = ? and "
                             "locale = ?", (english, locale))
            else:
                conn.execute(f"update {table} set {text} = ? where {key} = ? "
                             "and locale = ?", (corrected, english, locale))
        n = conn.execute(f"update {table} set source = 'verified' "
                         "where locale = ?", (locale,)).rowcount
        print(f"  {path.name}: {n} {target} verified for {locale!r}")


# ------------------------------------------------------------------ build --

OUTPUT_DDL = f"""
create table food_source (
    code          text primary key,
    name          text not null,
    license_note  text
) without rowid;

-- The food_summary materialized view. Untyped columns hold numerics as
-- PostgREST serialises them (integer when the text has no decimal point).
create table food_summary (
    food_id              integer primary key,
    source               text not null,
    source_code          text not null,
    name                 text not null,
    short_title          text,
    brands               text,
    barcode              text,
    category             text,
    serving_quantity,
    serving_unit         text,
    serving_size         text,
    serving_gram_weight,
    thumbnail_url        text,
    main_image_url       text,
    tags                 text,        -- JSON array, or NULL as array_agg gives
    {", ".join(f"{c}" for _, c in NUTRIENT_COLUMNS)},
    has_portion          integer not null,  -- 0/1
    -- helpers for the RPCs (not columns of the view)
    title_key            text not null,     -- lower(btrim(split_part(name, ',', 1)))
    default_portion_description text        -- first portion's raw description
);

create table food_translation (
    id            integer primary key,
    food_id       integer not null,
    locale        text not null,
    description   text not null,
    source        text not null,
    ai_generated  integer not null,
    title_key     text not null
);

-- Portions portions_by_food_ids can return, numbered as its row_number().
create table food_portion (
    food_id      integer not null,
    seq          integer not null,
    label_en     text not null,
    gram_weight  not null,
    primary key (food_id, seq)
) without rowid;

-- Seeded per distinct English text, as the seed migrations join them.
create table food_portion_translation (
    portion_description  text not null,
    locale               text not null,
    label                text,
    source               text not null,
    ai_generated         integer not null,
    primary key (portion_description, locale)
) without rowid;

create table measure_unit_translation (
    unit          text not null,
    locale        text not null,
    name          text not null,
    source        text not null,
    ai_generated  integer not null,
    primary key (unit, locale)
) without rowid;

create table meta (
    key    text primary key,
    value  text not null
) without rowid;

create virtual table food_fts using fts5(
    name, content='food_summary', content_rowid='food_id',
    tokenize="porter unicode61 tokenchars '/'", columnsize=0);

create virtual table food_translation_fts using fts5(
    description, content='food_translation', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2 tokenchars '/'", columnsize=0);
"""


def build_tables(conn, migrations: Path) -> None:
    conn.create_function("title_key", 1, title_key, deterministic=True)
    conn.create_function("is_deliverable", 1, is_deliverable,
                         deterministic=True)
    conn.create_function("serving_size", 3, serving_size, deterministic=True)

    conn.execute("""
        insert into food_source select code, name, license_note
        from stage.food_source order by code""")

    # Ranked portions: rn over all of a food's portions (the default serving
    # is rn = 1), seq over the deliverable ones (portions_by_food_ids).
    conn.executescript("""
        create temp table ranked_portion as
        select p.*,
               row_number() over (partition by p.food_id
                   order by p.seq_num is null, p.seq_num, p.id) as rn
        from stage.food_portion p;
        create index temp.ranked_portion_food on ranked_portion (food_id, rn);

        create temp table deliverable as
        select p.food_id, p.portion_description, p.gram_weight,
               row_number() over (partition by p.food_id
                   order by p.seq_num is null, p.seq_num, p.id) as seq
        from stage.food_portion p
        where p.portion_description is not null
          and p.portion_description <> 'Quantity not specified'
          and p.gram_weight is not null
          and p.gram_weight > 0
          and is_deliverable(p.portion_description);
        create index temp.deliverable_food on deliverable (food_id);

        create temp table nutrients as
        select food_id,
    """ + ",\n".join(
        f"max(case when nutrient_id = {i} then amount end) as {c}"
        for i, c in NUTRIENT_COLUMNS) + """
        from stage.food_nutrient group by food_id;
        create index temp.nutrients_food on nutrients (food_id);

        create temp table first_image as
        select food_id, kind,
               coalesce(external_url,
                        '/storage/v1/object/public/food-images/'
                        || storage_path) as url
        from (select i.*, row_number() over (partition by food_id, kind
                  order by id) as rn from stage.food_image i)
        where rn = 1;
    """)

    nutrient_cols = ", ".join(f"n.{c}" for _, c in NUTRIENT_COLUMNS)
    conn.execute(f"""
        insert into food_summary
        select f.id, f.source, f.source_code, f.description,
               coalesce(f.short_title, f.description),
               ma.brand_description, ma.upc_code, fc.description,
               p.amount,
               case when mu.name = 'undetermined' then 'portion'
                    else mu.name end,
               serving_size(p.portion_description, p.amount_text, p.modifier),
               p.gram_weight,
               it.url, im.url,
               null,
               {nutrient_cols},
               exists (select 1 from deliverable d where d.food_id = f.id),
               title_key(f.description),
               p.portion_description
        from stage.food f
        left join stage.food_category fc on fc.id = f.food_category_id
        left join stage.market_acquisition ma on ma.food_id = f.id
        left join ranked_portion p on p.food_id = f.id and p.rn = 1
        left join stage.measure_unit mu on mu.id = p.measure_unit_id
        left join first_image it on it.food_id = f.id and it.kind = 'thumbnail'
        left join first_image im on im.food_id = f.id and im.kind = 'main'
        left join nutrients n on n.food_id = f.id
        order by f.id""")

    conn.execute("""
        insert into food_portion
        select food_id, seq, portion_description, gram_weight
        from deliverable order by food_id, seq""")

    conn.execute("""
        insert into food_translation
            (food_id, locale, description, source, ai_generated, title_key)
        select food_id, locale, description, source, ai_generated,
               title_key(description)
        from stage.food_translation order by locale, food_id""")

    # Seeds: the migrations join on the text and skip conflicts; keep the
    # rows whose English text the loaded data actually carries.
    conn.execute("create index stage.food_portion_description "
                 "on food_portion (portion_description)")
    portion_rows = read_seed(migrations / PORTION_SEED)
    conn.executemany("""
        insert or ignore into food_portion_translation
        select ?1, ?2, ?3, 'machine', 1
        where exists (select 1 from stage.food_portion
                      where portion_description = ?1)""", portion_rows)
    unit_rows = read_seed(migrations / UNIT_SEED)
    conn.executemany("""
        insert or ignore into measure_unit_translation
        select ?1, ?2, ?3, 'machine', 1
        where exists (select 1 from stage.measure_unit where name = ?1)""",
                     unit_rows)

    for fts in ("food_fts", "food_translation_fts"):
        conn.execute(f"insert into {fts}({fts}) values ('rebuild')")
        conn.execute(f"insert into {fts}({fts}) values ('optimize')")


def write_meta(conn) -> dict:
    counts = dict(conn.execute(
        "select source, count(*) from food_summary group by source "
        "order by source").fetchall())
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "build_date": dt.datetime.now(dt.timezone.utc)
                        .replace(microsecond=0).isoformat()
                        .replace("+00:00", "Z"),
        "generator": "OpenNutriTracker-Backend scripts/shared/build_sqlite.py",
        "food_count": str(sum(counts.values())),
        "sources": json.dumps(counts, separators=(",", ":")),
        "translation_count": str(conn.execute(
            "select count(*) from food_translation").fetchone()[0]),
        "translation_locales": json.dumps(dict(conn.execute(
            "select locale, count(*) from food_translation group by locale "
            "order by locale").fetchall()), separators=(",", ":")),
        "portion_count": str(conn.execute(
            "select count(*) from food_portion").fetchone()[0]),
        "fts_english": FTS_ENGLISH,
        "fts_translation": FTS_TRANSLATION,
    }
    conn.executemany("insert into meta values (?, ?)", meta.items())
    return meta


def compact(path: Path, page_size: int | str) -> int:
    """VACUUM at the chosen (or smallest-file) page size; returns it."""
    candidates = PAGE_SIZES if page_size == "auto" else (int(page_size),)
    best = None
    for size in candidates:
        conn = sqlite3.connect(path)
        conn.execute(f"pragma page_size = {size}")
        conn.execute("vacuum")
        conn.close()
        length = path.stat().st_size
        print(f"  page_size {size:>5}: {length:>12,} bytes")
        if best is None or length < best[1]:
            best = (size, length)
    if len(candidates) > 1 and best[0] != candidates[-1]:
        conn = sqlite3.connect(path)
        conn.execute(f"pragma page_size = {best[0]}")
        conn.execute("vacuum")
        conn.close()
    return best[0]


def write_attribution(conn, path: Path, meta: dict) -> None:
    counts = json.loads(meta["sources"])
    notes = dict(conn.execute(
        "select code, license_note from food_source").fetchall())
    names = dict(conn.execute("select code, name from food_source").fetchall())
    lines = [
        "# Attribution",
        "",
        f"`foods.sqlite` (schema {meta['schema_version']}, built "
        f"{meta['build_date']}) combines the food composition databases below,"
        " converted by [OpenNutriTracker-Backend]"
        "(https://github.com/simonoppowa/OpenNutriTracker-Backend) into one "
        "schema: names kept, nutrient values unit-normalized as each "
        "converter's `nutrient_mapping` documents. Portion and unit "
        "translations are the repository's machine-translated seeds "
        "(`sql/migrations/*_translation_seed.sql`).",
        "",
        "| source | foods | dataset | licence | attribution |",
        "|---|---:|---|---|---|",
    ]
    for code in sorted(counts):
        key = code if code in ATTRIBUTION else code.split("_")[0]
        dataset, licence, credit = ATTRIBUTION.get(
            key, (names.get(code, code), "see licence note", ""))
        lines.append(f"| `{code}` | {counts[code]:,} | {dataset} | {licence} "
                     f"| {credit} |")
    lines += ["", "Licence notes as each converter records them "
              "(`food_source.license_note`):", ""]
    for code in sorted(counts):
        lines.append(f"- `{code}` -- {names.get(code, code)}: "
                     f"{notes.get(code) or '(none)'}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def build(csv_dirs: list[Path], output: Path, migrations: Path,
          reviews: list[Path] = (), page_size: int | str = "auto",
          gz: bool = True, attribution: bool = True) -> dict:
    out_dir = output.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("pragma journal_mode = off")
        conn.execute("pragma synchronous = off")
        conn.execute("attach database '' as stage")
        conn.executescript(STAGE_DDL)
        load_csv_dirs(conn, csv_dirs)
        check_references(conn)
        conn.executescript(OUTPUT_DDL)
        build_tables(conn, migrations)
        apply_reviews(conn, list(reviews))
        meta = write_meta(conn)
        conn.commit()
        conn.execute("detach database stage")
        conn.execute(f"pragma user_version = {SCHEMA_VERSION}")
        conn.execute("analyze")
        conn.commit()
    finally:
        conn.close()
    print("compacting")
    chosen = compact(tmp, page_size)
    conn = sqlite3.connect(tmp)
    conn.execute("pragma journal_mode = delete")
    problems = conn.execute("pragma integrity_check").fetchall()
    if problems != [("ok",)]:
        conn.close()
        raise BuildError(f"integrity_check: {problems}")
    if attribution:
        write_attribution(conn, out_dir / "ATTRIBUTION.md", meta)
    conn.close()
    os.replace(tmp, output)
    print(f"wrote {output} ({output.stat().st_size:,} bytes, page_size "
          f"{chosen})")
    if gz:
        gz_path = output.with_name(output.name + ".gz")
        with open(output, "rb") as src, open(gz_path, "wb") as raw:
            # mtime 0 and no file name: the same database gives the same .gz
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                               compresslevel=9, mtime=0) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        print(f"wrote {gz_path} ({gz_path.stat().st_size:,} bytes)")
    print(f"foods per source: {meta['sources']}")
    return meta


# -------------------------------------------------------------- the RPCs --

def websearch_to_fts5(term: str | None, english: bool) -> str | None:
    """websearch_to_tsquery syntax as an FTS5 MATCH expression.

    Unquoted words are ANDed, "quoted text" is a phrase, `or` between terms
    is OR and a leading `-` is NOT, as in Postgres. Each word or phrase
    becomes an FTS5 string, which FTS5 tokenizes with the table's own
    tokenizer, so stemming and diacritic folding apply on both sides.

    Where this differs from Postgres (README, "Offline SQLite database",
    lists every difference, the tokenizers' included):
    * english: stopwords are dropped from the query here (Postgres drops
      them from both sides); inside a phrase only leading/trailing ones go,
      inner ones must be adjacent where Postgres leaves a position gap.
    * a group made only of negations (`-cheese`) matches everything in
      Postgres; FTS5 has no unary NOT, so the group is dropped.
    * None when nothing searchable is left: Postgres's empty tsquery, which
      matches no row.
    """
    if term is None:
        return None
    groups: list[list[tuple[bool, list[str]]]] = [[]]
    i, n = 0, len(term)
    while i < n:
        c = term[i]
        if c.isspace():
            i += 1
            continue
        negated = False
        if c == "-" and i + 1 < n and not term[i + 1].isspace():
            negated = True
            i += 1
        if term[i] == '"':
            end = term.find('"', i + 1)
            end = n if end < 0 else end
            text, i, quoted = term[i + 1:end], end + 1, True
        else:
            j = i
            while j < n and not term[j].isspace() and term[j] != '"':
                j += 1
            text, i, quoted = term[i:j], j, False
        if not quoted and not negated and text.lower() == "or":
            groups.append([])
            continue
        words = _WORD.findall(text)
        if english:
            while words and words[0].lower() in ENGLISH_STOPWORDS:
                words.pop(0)
            while words and words[-1].lower() in ENGLISH_STOPWORDS:
                words.pop()
        if words:
            groups[-1].append((negated, words))
    exprs = []
    for group in groups:
        pos = ['"' + " ".join(w) + '"' for neg, w in group if not neg]
        neg = ['"' + " ".join(w) + '"' for neg, w in group if neg]
        if not pos:
            continue
        expr = "(" + " AND ".join(pos) + ")"
        for x in neg:
            expr += " NOT " + x
        exprs.append("(" + expr + ")")
    return " OR ".join(exprs) if exprs else None


def pg_btrim_lower(term: str) -> str:
    return term.strip(" ").lower()


class OfflineRpc:
    """Reference implementation of the five RPCs against foods.sqlite.

    The app's SqliteFoodRpcClient runs the same statements. Each returns
    rows as dicts keyed like the Postgres function's columns.
    """

    SUMMARY_SELECT = ", ".join(f"fs.{c}" for c in SUMMARY_COLUMNS)

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.row_factory = sqlite3.Row

    def _rows(self, sql, params):
        out = []
        for r in self.conn.execute(sql, params):
            d = dict(r)
            for k in ("has_portion", "localized"):
                if k in d and d[k] is not None:
                    d[k] = bool(d[k])
            if d.get("tags") is not None:
                d["tags"] = json.loads(d["tags"])
            out.append(d)
        return out

    def search_food_summary(self, term, sources=None, max_rows=100):
        match = websearch_to_fts5(term, english=True)
        if match is None:
            return []
        return self._rows(f"""
            select {self.SUMMARY_SELECT}
            from food_fts join food_summary fs on fs.food_id = food_fts.rowid
            where food_fts match ?1
              and (?2 is null or fs.source in (select value from json_each(?2)))
            order by fs.has_portion desc, (fs.title_key = ?3) desc,
                     length(fs.name), fs.food_id
            limit max(coalesce(?4, 0), 0)""",
            (match, None if sources is None else json.dumps(sources),
             pg_btrim_lower(term), max_rows))

    def search_food_translation(self, term, loc, max_rows=100):
        match = websearch_to_fts5(term, english=False)
        if match is None:
            return []
        return self._rows("""
            select ft.food_id, ft.description, ft.source,
                   coalesce(fs.has_portion, 0) as has_portion
            from food_translation_fts
            join food_translation ft on ft.id = food_translation_fts.rowid
            left join food_summary fs on fs.food_id = ft.food_id
            where food_translation_fts match ?1 and ft.locale = ?2
            order by coalesce(fs.has_portion, 0) desc, (ft.title_key = ?3) desc,
                     length(ft.description), ft.food_id
            limit max(coalesce(?4, 0), 0)""",
            (match, loc, pg_btrim_lower(term), max_rows))

    def food_summary_by_ids(self, ids, sources=None):
        return self._rows(f"""
            select {self.SUMMARY_SELECT} from food_summary fs
            where fs.food_id in (select value from json_each(?1))
              and (?2 is null or fs.source in (select value from json_each(?2)))
            order by fs.food_id""",
            (json.dumps(ids), None if sources is None else json.dumps(sources)))

    def portion_labels_by_food_ids(self, ids, loc):
        return self._rows("""
            select j.value as food_id, t.label as label
            from json_each(?1) j
            join food_summary fs on fs.food_id = j.value
            join food_portion_translation t
              on t.portion_description = fs.default_portion_description
             and t.locale = ?2 and t.source = 'verified'
            where t.label is not null and trim(t.label, ' ') <> ''
            order by j.key""", (json.dumps(ids), loc))

    def portions_by_food_ids(self, ids, loc):
        return self._rows("""
            select p.food_id, p.seq, coalesce(t.label, p.label_en) as label,
                   t.label is not null as localized, p.gram_weight,
                   p.label_en
            from food_portion p
            left join food_portion_translation t
              on t.portion_description = p.label_en and t.locale = ?2
             and t.source = 'verified' and t.label is not null
             and trim(t.label, ' ') <> ''
            where p.food_id in (select value from json_each(?1))
            order by p.food_id, p.seq""", (json.dumps(ids), loc))


# ------------------------------------------------------------------ check --

def run_checks(path: Path) -> int:
    """Sample searches on a real build; exits non-zero on any failure."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rpc = OfflineRpc(conn)
    meta = dict(conn.execute("select key, value from meta").fetchall())
    sources = json.loads(meta["sources"])
    failures = 0

    def check(label, ok, detail=""):
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'}  {label}" +
              (f"  ({detail})" if detail and not ok else ""))
        failures += 0 if ok else 1

    check("schema version", meta.get("schema_version") == str(SCHEMA_VERSION),
          meta.get("schema_version"))
    check("integrity", conn.execute("pragma quick_check").fetchone()[0] == "ok")

    def first(rows, key="name"):
        return rows[0][key] if rows else None

    if any(s.startswith("fdc") for s in sources):
        rows = rpc.search_food_summary("potato")
        check("potato: portioned rows first, exact title first",
              rows and rows[0]["has_portion"] and
              title_key(rows[0]["name"]) == "potato", first(rows))
        check("potato: 100-row cap", len(rows) == 100, len(rows))
        keys = [(not r["has_portion"], title_key(r["name"]) != "potato",
                 len(r["name"]), r["food_id"]) for r in rows]
        check("potato: rows in the RPC's order", keys == sorted(keys))
        rows = rpc.search_food_summary("hummus", sources=["fdc_foundation"])
        hummus = [r for r in rows if r["food_id"] == 321358]
        check("hummus 321358 magnesium 71.1",
              hummus and hummus[0]["magnesium_100"] == 71.1,
              hummus[:1])
        check("source filter", rows and all(
            r["source"] == "fdc_foundation" for r in rows))
        rows = rpc.search_food_summary("pears -juice", max_rows=1000)
        check("websearch NOT",
              rows and not any("juice" in r["name"].lower() for r in rows),
              first(rows))
        both = {r["food_id"] for r in rpc.search_food_summary(
            "pear or plum", max_rows=1000)}
        check("websearch OR", both >= {r["food_id"] for r in rows} and
              len(both) > len(rows))
        check("stopword-only term matches nothing",
              rpc.search_food_summary("the with") == [])
        ids = [r["food_id"] for r in rpc.search_food_summary("milk")[:5]]
        portions = rpc.portions_by_food_ids(ids, "de")
        check("portions_by_food_ids numbered from 1", portions and all(
            p["seq"] >= 1 and p["gram_weight"] > 0 and p["label_en"]
            for p in portions))
        check("portions_by_food_ids not localized (seeds are machine)",
              not any(p["localized"] for p in portions))
        check("portion_labels_by_food_ids: no verified labels",
              rpc.portion_labels_by_food_ids(ids, "de") == [])
    if "bls" in sources:
        rows = rpc.search_food_summary("oat flakes", sources=["bls"])
        oat = [r for r in rows if r["source_code"] == "C133000"]
        check("BLS C133000 oat flakes 348 kcal",
              oat and oat[0]["energy_kcal_100"] == 348, oat[:1])
        rows = rpc.search_food_translation("Hafer Flocken", "de")
        check("de translation search",
              any(r["food_id"] == 10000001 for r in rows), rows[:3])
    if "slv" in sources:
        rows = rpc.search_food_summary("apple skin", sources=["slv"])
        apple = [r for r in rows if r["source_code"] == "588"]
        check("SLV 588 apple with skin 48 kcal",
              apple and apple[0]["energy_kcal_100"] == 48, apple[:1])
        rows = rpc.search_food_translation("äpple", "sv")
        check("sv translation search (diacritics)", bool(rows))
        check("diacritics folded (apple matches äpple)",
              len(rpc.search_food_translation("apple", "sv")) >= len(rows))
    if "ciqual" in sources:
        rows = rpc.search_food_translation("pomme", "fr")
        check("fr translation search", bool(rows))
    if "fineli" in sources:
        rows = rpc.search_food_summary("rye bread", sources=["fineli"])
        check("fineli English search", bool(rows))
        check("fineli portions", bool(rpc.portions_by_food_ids(
            [r["food_id"] for r in rows[:3]], None)))
    if "mvt" in sources:
        check("nb translation search",
              bool(rpc.search_food_translation("brød", "nb")))
    if "frida" in sources:
        check("da translation search",
              bool(rpc.search_food_translation("æble", "da")))
    print(f"\n{failures} failure(s)")
    conn.close()
    return 1 if failures else 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build the offline foods.sqlite from converted CSVs")
    ap.add_argument("--csv-dir", nargs="+", type=Path, default=[],
                    help="converted CSV folders (out/fdc out/bls ...)")
    ap.add_argument("--output", type=Path, default=Path("out/sqlite/foods.sqlite"),
                    help="database to write (default out/sqlite/foods.sqlite)")
    ap.add_argument("--migrations", type=Path, default=DEFAULT_MIGRATIONS,
                    help="sql/migrations, for the translation seeds")
    ap.add_argument("--page-size", default="auto",
                    help="SQLite page size, or 'auto' to keep the smallest "
                         f"file of {PAGE_SIZES} (default)")
    ap.add_argument("--review", nargs="+", type=Path, default=[],
                    metavar="CSV",
                    help="reviewed portions_<locale>.csv / units_<locale>.csv "
                         "from review_translations.py --export: promote "
                         "those locales to 'verified'")
    ap.add_argument("--no-attribution", action="store_true",
                    help="skip ATTRIBUTION.md (e.g. for test fixtures)")
    ap.add_argument("--no-gzip", action="store_true",
                    help="skip foods.sqlite.gz")
    ap.add_argument("--check", type=Path, metavar="DB",
                    help="run sample searches against a built database and "
                         "exit")
    args = ap.parse_args()

    try:
        if args.check:
            sys.exit(run_checks(args.check))
        if not args.csv_dir:
            ap.error("--csv-dir is required (or --check DB)")
        build(args.csv_dir, args.output, args.migrations, args.review,
              page_size=args.page_size, gz=not args.no_gzip,
              attribution=not args.no_attribution)
    except BuildError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
