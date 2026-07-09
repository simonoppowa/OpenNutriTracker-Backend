#!/usr/bin/env python3
"""Convert web-scraped TBCA data (Tabela Brasileira de Composicao de
Alimentos) into CSV files matching schema.sql - same tables the FDC /
BLS / INDB converters fill.

Input: the scraped tbca_foods.csv + tbca_nutrients_wide.csv (the wide
file already carries per-100g columns named after the app's fields).

Emitted CSVs:

    food_source.csv       (1 row: tbca)
    nutrient_mapping.csv  (source='tbca', wide-column names as codes)
    food_category.csv     (TBCA groups, ids 30001+, source='tbca')
    food.csv              (ids start at 30_000_000)
    food_alias.csv        (scientific names as searchable aliases)
    food_nutrient.csv     (canonical nutrients)

No portions (TBCA scrape has none) and no translations: the scraped
descriptions are English. If you scrape the Portuguese descriptions
too, load them into food_translation (locale 'pt-BR', source 'native').

Usage:
    python tbca_to_ont_csv.py \
        --foods tbca_foods.csv \
        --nutrients tbca_nutrients_wide.csv \
        --output <out_dir>

Import (append, after FDC):
    python import_fdc.py --csv-dir <out_dir> --append

Notes
-----
* id ranges: FDC < 10M, BLS 10M+, INDB 20M+, TBCA 30M+.
* Category ids: FDC 1-28, WWEIA 1002-9999, TBCA 30001+.
* 'sugars' stays NULL: TBCA reports added sugar only, which is not
  total sugars - mapping it would understate real values.
* Vitamin A is TBCA's mcg RE, stored as canonical vitamin_a_rae
  (close, but RE slightly overstates RAE for plant foods).
* TBCA (www.tbca.net.br, USP/FoRC) allows use with attribution.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

FOOD_ID_BASE = 30_000_000
NUTRIENT_ID_BASE = 40_000_000
CATEGORY_ID_BASE = 30_000

# canonical nutrient id -> column in tbca_nutrients_wide.csv
WIDE_MAPPING = [
    (1,  "energyKcal100"),
    (2,  "carbohydrates100"),        # available carbohydrates
    (3,  "fat100"),
    (4,  "proteins100"),
    # 5 sugars: TBCA has added sugar only -> NULL
    (6,  "saturatedFat100"),
    (7,  "fiber100"),
    (8,  "monounsaturatedFat100"),
    (9,  "polyunsaturatedFat100"),
    (10, "transFat100"),
    (11, "cholesterol100"),
    (12, "sodium100"),
    (13, "potassium100"),
    (14, "magnesium100"),
    (15, "calcium100"),
    (16, "iron100"),
    (17, "zinc100"),
    (18, "phosphorus100"),
    (19, "vitaminA100"),             # mcg RE ~ RAE
    (20, "vitaminC100"),
    (21, "vitaminD100"),
    (22, "vitaminB6100"),
    (23, "vitaminB12100"),
    (24, "niacin100"),
]


def val(s: str | None) -> float | None:
    if s is None or not s.strip():
        return None
    try:
        return float(s.strip().replace(",", "."))
    except ValueError:
        return None


def short_title(description: str) -> str:
    """Concise display name: text before the first comma, capped at 80
    ('Adzuki beans, sweet' -> 'Adzuki beans')."""
    head = description.split(",")[0].strip()
    return (head or description.strip())[:80]


def convert(foods_csv: Path, nutrients_csv: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(foods_csv, newline="", encoding="utf-8-sig") as fh:
        food_rows = list(csv.DictReader(fh))
    with open(nutrients_csv, newline="", encoding="utf-8-sig") as fh:
        wide_reader = csv.DictReader(fh)
        wide_cols = wide_reader.fieldnames or []
        wide = {r["code"]: r for r in wide_reader}

    mapping = [(nid, c) for nid, c in WIDE_MAPPING if c in wide_cols]
    skipped_cols = [c for _, c in WIDE_MAPPING if c not in wide_cols]
    if skipped_cols:
        print(f"  note: columns missing in wide file: {skipped_cols}",
              file=sys.stderr)

    categories: dict[str, int] = {}
    foods, aliases, food_nutrients = [], [], []
    seen: set[str] = set()
    next_fn_id = NUTRIENT_ID_BASE

    for r in food_rows:
        code = (r.get("code") or "").strip()
        name = (r.get("description") or "").strip()
        if not code or code in seen or not name:
            continue
        seen.add(code)
        food_id = FOOD_ID_BASE + len(foods)

        group = (r.get("group") or "").strip()
        cat_id = ""
        if group:
            if group not in categories:
                categories[group] = CATEGORY_ID_BASE + len(categories) + 1
            cat_id = categories[group]

        foods.append({
            "id": food_id,
            "source": "tbca",
            "source_code": code,
            "description": name,
            "short_title": short_title(name),
            "food_category_id": cat_id,
            "publication_date": "",
        })

        sci = (r.get("scientific_name") or "").strip()
        if sci:
            aliases.append({
                "food_id": food_id,
                "locale": "la",           # Latin binomial, searchable
                "alias": sci,
                "source": "derived",
            })

        w = wide.get(code)
        if not w:
            continue
        for nid, colname in mapping:
            amount = val(w.get(colname))
            if amount is None or amount < 0:
                continue
            food_nutrients.append({
                "id": next_fn_id,
                "food_id": food_id,
                "nutrient_id": nid,
                "amount": amount,
                "origin": "literature",   # TBCA compiles analytical data
                "reference": "",
                "data_points": "", "min": "", "max": "",
            })
            next_fn_id += 1

    no_nutrients = len(seen) - len({fn["food_id"] for fn in food_nutrients})
    if no_nutrients:
        print(f"  note: {no_nutrients} foods without any nutrient values",
              file=sys.stderr)

    # ------------------------------------------------------------------ write
    def write(name: str, rows_, columns: list[str]) -> None:
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_)
        print(f"  {name:20s} {len(rows_):>7,} rows")

    print("Writing Supabase CSVs:")
    write("food_source",
          [{"code": "tbca",
            "name": "Tabela Brasileira de Composicao de Alimentos (TBCA)",
            "license_note": "Free with attribution - www.tbca.net.br (USP/FoRC)"}],
          ["code", "name", "license_note"])
    write("nutrient_mapping",
          [{"source": "tbca", "source_component_code": c,
            "nutrient_id": nid, "unit_factor": 1} for nid, c in WIDE_MAPPING],
          ["source", "source_component_code", "nutrient_id", "unit_factor"])
    write("food_category",
          [{"id": cid, "source": "tbca", "description": g}
           for g, cid in sorted(categories.items(), key=lambda kv: kv[1])],
          ["id", "source", "description"])
    write("food", foods,
          ["id", "source", "source_code", "description", "short_title",
           "food_category_id", "publication_date"])
    write("food_alias", aliases, ["food_id", "locale", "alias", "source"])
    write("food_nutrient", food_nutrients,
          ["id", "food_id", "nutrient_id", "amount", "origin",
           "reference", "data_points", "min", "max"])

    n_energy = len({fn["food_id"] for fn in food_nutrients
                    if fn["nutrient_id"] == 1})
    print(f"\nDone. {len(foods):,} TBCA foods ({n_energy:,} with energy, "
          f"{len(categories)} categories, {len(aliases):,} aliases) -> {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert scraped TBCA CSVs to multi-source Supabase CSVs")
    ap.add_argument("--foods", required=True, type=Path, help="tbca_foods.csv")
    ap.add_argument("--nutrients", required=True, type=Path,
                    help="tbca_nutrients_wide.csv")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    for f in (args.foods, args.nutrients):
        if not f.exists():
            sys.exit(f"error: {f} not found")
    convert(args.foods, args.nutrients, args.output)


if __name__ == "__main__":
    main()
