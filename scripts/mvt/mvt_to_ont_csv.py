#!/usr/bin/env python3
"""Convert the Norwegian Food Composition Table (Matvaretabellen, by
Mattilsynet) JSON exports into CSV files matching the multi-source
Supabase schema (multisource_food_schema.mermaid) - the same tables the
FDC / BLS / INDB / TBCA converters fill.

Emitted CSVs:

    food_source.csv       (1 row: mvt)
    nutrient_mapping.csv  (source='mvt'; MVT nutrientIds as codes)
    food_category.csv     (MVT food groups, ids 80001+, source='mvt')
    food.csv              (ids start at 80_000_000; English names)
    food_translation.csv  (native Norwegian Bokmaal names, locale 'nb')
    food_portion.csv      (household measures, ids 80_000_000+)
    food_nutrient.csv     (canonical nutrients, ids 90_000_000+)

NOT emitted: nutrient.csv (shared canonical list, ships with the FDC
conversion) and measure_unit.csv (portions reference FDC's unit 9999
'undetermined', exactly like INDB; load the FDC CSVs first).

Usage:
    # with the JSON files under ./source/mvt (download_mvt.py):
    python mvt_to_ont_csv.py --output <out_dir>

    # or point at another folder:
    python mvt_to_ont_csv.py --source-dir /data/mvt --output <out_dir>

Typical pipeline:
    python download_mvt.py --output source           # -> ./source/mvt
    python mvt_to_ont_csv.py --output ../../out/mvt  # reads ./source/mvt

Import (append, after FDC):
    python shared/import_fdc.py --csv-dir out/mvt --append

Notes
-----
* id ranges: food 80M+, food_nutrient 90M+, food_portion 80M+,
  food_category 80001+ (CATEGORY_ID_BASE 80_000). Foods are numbered in
  foodId order, categories in natural food-group-id order (1, 1.1,
  1.4, 1.4.4 ...), so a re-run on the same export gives the same ids.
* Names: en-foods.json carries the official English name
  (food.description); nb-foods.json the Bokmaal name, stored as
  food_translation (locale 'nb', source 'native'). Both exports share
  foodIds and identical constituent values.
* Energy: canonical is kcal. MVT publishes kcal directly ('calories',
  computed by Mattilsynet with kcal factors) -> unit_factor 1. Its kJ
  value ('energy') is computed separately with kJ factors, so kJ/4.184
  differs from the published kcal by ~1%; kJ is only a fallback
  (x 1/4.184 = 0.239006) for a food without a kcal value (none in the
  2026 export).
* Vitamin A: canonical is ug RAE. MVT 'Vit A' is RAE (unit 'RAE' = ug
  retinol activity equivalents, retinol + beta-carotene/12) -> factor 1.
  'Vit A RE' (beta-carotene/6) is NOT used as a fallback: RE overstates
  RAE, and no food has RE without RAE anyway.
* Sugars = 'Mono+Di' (EuroFIR SUGAR, total sugars). NOT 'Sukker' (added
  sugar, SUGAD) or 'SUGAN' (free sugar).
* Carbohydrates = 'Karbo' (EuroFIR CHO, available: sugar + starch).
* Niacin = 'Niacin' (preformed, EuroFIR NIA), matching FDC's 1167 -
  not 'NIAEQ' (niacin equivalents incl. tryptophan/60).
* Every other mapped nutrient is already in the canonical unit
  (g / mg / ug) -> factor 1. Each constituent's own 'unit' is checked
  against the expected one; a mismatch is skipped (left NULL) with a
  warning instead of being stored in the wrong unit.
* Nullable = honest: a constituent without a 'quantity' (MVT sourceId
  '10', "missing value") stays NULL. Published zeros (e.g. sourceId
  '50', "naturally occurring zero, not analysed") stay 0.
* All 24 canonical nutrients have an MVT component (incl. trans fat and
  B12); per-food gaps are the only NULLs.
* origin: derived from the provenance code (sourceId) via
  en-sources.json - MI* method codes, estimates, recipes, aggregates
  -> calculated; analyses -> analysis; bibliographic references (USDA,
  McCance & Widdowson, other food tables) -> literature; unspecified
  industry data -> '' (unknown). food_nutrient.reference = the sourceId.
* Portions: every MVT portion ('portions', grams per 1 unit) becomes a
  food_portion row (amount 1, measure_unit 9999, portion_description
  "1 <English portion name>" - the wording matvaretabellen.no shows,
  e.g. "1 glass (large)"). The export's portion order is not stable,
  so seq_num follows PORTION_PRIORITY: natural single servings first
  (portion, pcs, slice, glass ...), volume/spoon measures last. seq_num
  1 is the app's default serving. Norwegian portion names are not
  emitted (food_portion_translation is not loaded by import_fdc.py).
* License: Mattilsynet's terms page states CC BY 4.0
  (https://www.mattilsynet.no/mat-og-drikke/matvaretabellen/opphavsrett;
  data.norge.no lists NLOD 2.0). Required attribution:
  "Matvaretabellen 2026. Mattilsynet. www.matvaretabellen.no".
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

FOOD_ID_BASE = 80_000_000
NUTRIENT_ID_BASE = 90_000_000      # food_nutrient ids
PORTION_ID_BASE = 80_000_000       # food_portion ids
CATEGORY_ID_BASE = 80_000          # food_category ids 80001+
UNDETERMINED_UNIT_ID = 9999        # FDC measure_unit 'undetermined'
DEFAULT_SOURCE_DIR = Path("source") / "mvt"   # what download_mvt.py fills
EDITION = "2026"                   # bump with each annual release

KJ_TO_KCAL = round(1 / 4.184, 6)   # 0.239006

# ---------------------------------------------------------------------------
# canonical nutrient id -> [(MVT code, expected MVT unit, unit_factor), ...]
# in priority order (first present per food wins). Codes are MVT
# 'nutrientId's, except 'calories' / 'energy' (top-level food fields).
# ---------------------------------------------------------------------------
MVT_MAPPING = [
    (1,  [("calories", "kcal", 1),            # published kcal
          ("energy",   "kJ",   KJ_TO_KCAL)]),  # fallback: kJ / 4.184
    (2,  [("Karbo",    "g",    1)]),   # available carbohydrate (CHO)
    (3,  [("Fett",     "g",    1)]),   # fat, total
    (4,  [("Protein",  "g",    1)]),
    (5,  [("Mono+Di",  "g",    1)]),   # total sugars (NOT 'Sukker' = added)
    (6,  [("Mettet",   "g",    1)]),   # saturated fatty acids
    (7,  [("Fiber",    "g",    1)]),   # dietary fibre, total
    (8,  [("Enumet",   "g",    1)]),   # monounsaturated
    (9,  [("Flerum",   "g",    1)]),   # polyunsaturated
    (10, [("Trans",    "g",    1)]),   # trans fatty acids
    (11, [("Kolest",   "mg",   1)]),   # cholesterol
    (12, [("Na",       "mg",   1)]),
    (13, [("K",        "mg",   1)]),
    (14, [("Mg",       "mg",   1)]),
    (15, [("Ca",       "mg",   1)]),
    (16, [("Fe",       "mg",   1)]),
    (17, [("Zn",       "mg",   1)]),
    (18, [("P",        "mg",   1)]),
    (19, [("Vit A",    "RAE",  1)]),   # ug RAE (no RE fallback)
    (20, [("Vit C",    "mg",   1)]),
    (21, [("Vit D",    "µg",   1)]),
    (22, [("Vit B6",   "mg",   1)]),
    (23, [("Vit B12",  "µg",   1)]),
    (24, [("Niacin",   "mg",   1)]),   # preformed niacin (NOT NIAEQ)
]

# Default-serving order for food_portion.seq_num (MVT portion ids).
# Unlisted ids follow, alphabetically.
PORTION_PRIORITY = [
    "porsjon", "stk", "stk_middels", "skive", "glass", "beger", "kopp",
    "filet", "stk_liten", "stk_stor", "ferdig_skivet", "pr_skive",
    "glass_lite", "glass_stort", "plate", "plate_middels", "plate_liten",
    "plate_stor", "stang", "boks", "boks_liten", "pakke", "pose",
    "pose_liten", "pose_stor", "kartong", "bukett", "baat", "ring",
    "stilk", "blad", "fedd", "terning", "cm_rot", "neve", "dl",
    "spiseskje", "teskje",
]

# provenance keywords (en-sources.json descriptions), checked in order
CALCULATED_HINTS = ("calculat", "estimat", "aggregat", "imput", "recipe",
                    "assumed", "mean value", "weighted", "similar food")
ANALYSIS_HINTS = ("analysed", "analyzed", "nutrient analys",
                  "analysis of nutrients", "analyses of")


def short_title(description: str) -> str:
    """Concise display name: text before the first comma, capped at 80."""
    head = description.split(",")[0].strip()
    return (head or description.strip())[:80]


def norm_unit(unit) -> str:
    """'μg' (Greek mu), 'µg' (micro sign), 'ug', 'mcg' -> 'µg'."""
    u = str(unit or "").strip().replace("μ", "µ")
    return "µg" if u.lower() in ("ug", "mcg") else u


def raw_values(food: dict) -> dict:
    """MVT code -> (quantity, unit, sourceId) for one food: every
    constituent with a numeric quantity, plus the top-level 'calories'
    (kcal) and 'energy' (kJ). Missing quantities are left out (NULL)."""
    out = {}
    entries = [(c.get("nutrientId"), c) for c in food.get("constituents", [])]
    entries += [("calories", food.get("calories") or {}),
                ("energy", food.get("energy") or {})]
    for code, c in entries:
        q = c.get("quantity")
        if code and isinstance(q, (int, float)) and not isinstance(q, bool):
            out[code] = (float(q), norm_unit(c.get("unit")),
                         str(c.get("sourceId") or ""))
    return out


def resolve(values: dict, options, warn=None):
    """First mapped option present with the expected unit ->
    (amount in canonical unit, code, sourceId), else None."""
    for code, unit, factor in options:
        hit = values.get(code)
        if hit is None:
            continue
        q, u, src = hit
        if u != norm_unit(unit):
            if warn is not None:
                warn.add(f"{code}: unit {u!r} (expected {unit!r})")
            continue
        return round(q * factor, 6), code, src
    return None


def origin_for(source_id: str, desc: str) -> str:
    """Harmonize an MVT provenance code onto analysis | literature |
    calculated | '' (unknown)."""
    d = desc.lower()
    if source_id.upper().startswith("MI") or any(h in d for h in CALCULATED_HINTS):
        return "calculated"
    if any(h in d for h in ANALYSIS_HINTS):
        return "analysis"
    if not d or "data from the industry" in d or "missing value" in d:
        return ""
    return "literature"


def group_key(group_id: str):
    """Natural order for food-group ids: '1' < '1.4' < '1.4.10' < '2'."""
    return tuple(int(p) if p.isdigit() else p for p in group_id.split("."))


def portion_rank(portion_id: str):
    try:
        return (0, PORTION_PRIORITY.index(portion_id), "")
    except ValueError:
        return (1, 0, portion_id)


def load(path: Path, key: str) -> list:
    if not path.exists():
        sys.exit(f"error: {path} not found - run download_mvt.py first, "
                 f"or pass --source-dir")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"error: {path} is not valid JSON ({e}) - re-download it "
                 f"with download_mvt.py")
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        sys.exit(f"error: {path} has no '{key}' list - unexpected format")
    return data[key]


def convert(src: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    foods_en = load(src / "en-foods.json", "foods")
    # foodIds/names occasionally carry stray whitespace ('01.332 ') - strip
    foods_nb = {str(f.get("foodId") or "").strip(): f
                for f in load(src / "nb-foods.json", "foods")}
    groups = load(src / "en-food-groups.json", "foodGroups")
    sources_path = src / "en-sources.json"
    source_desc = ({s["sourceId"]: s.get("description", "")
                    for s in load(sources_path, "sources")}
                   if sources_path.exists() else {})
    if not source_desc:
        print("  note: en-sources.json missing - origin left empty",
              file=sys.stderr)

    # nutrient catalog sanity check: the mapped codes still carry the units
    # this converter expects (per-value units are checked again below)
    nut_path = src / "en-nutrients.json"
    if nut_path.exists():
        catalog = {n["nutrientId"]: norm_unit(n.get("unit"))
                   for n in load(nut_path, "nutrients")}
        for nid, options in MVT_MAPPING:
            for code, unit, _f in options:
                if code in catalog and catalog[code] != norm_unit(unit):
                    print(f"  WARNING: catalog unit for {code} is "
                          f"{catalog[code]!r}, expected {unit!r} - those "
                          f"values will be skipped", file=sys.stderr)

    # ---- food groups -> food_category ---------------------------------------
    categories = []
    cat_id = {}
    for g in sorted(groups, key=lambda g: group_key(g["foodGroupId"])):
        gid = g["foodGroupId"]
        cat_id[gid] = CATEGORY_ID_BASE + len(categories) + 1
        categories.append({"id": cat_id[gid], "source": "mvt",
                           "description": g["name"].strip()})

    foods, translations, portions, food_nutrients = [], [], [], []
    seen: set[str] = set()
    unit_warnings: set[str] = set()
    skipped_portions = 0
    unknown_groups: set[str] = set()
    next_fn_id = NUTRIENT_ID_BASE

    for f in sorted(foods_en, key=lambda f: str(f.get("foodId") or "").strip()):
        code = str(f.get("foodId") or "").strip()
        name_en = str(f.get("foodName") or "").strip()
        if not code or code in seen or not name_en:
            continue
        seen.add(code)
        food_id = FOOD_ID_BASE + len(foods)
        gid = str(f.get("foodGroupId") or "")
        if gid and gid not in cat_id:
            unknown_groups.add(gid)
        foods.append({
            "id": food_id,
            "source": "mvt",
            "source_code": code,
            "description": name_en,
            "short_title": short_title(name_en),
            "food_category_id": cat_id.get(gid, ""),
            "publication_date": "",
        })
        name_nb = str((foods_nb.get(code) or {}).get("foodName") or "").strip()
        if name_nb:
            translations.append({
                "food_id": food_id,
                "locale": "nb",
                "description": name_nb,
                "source": "native",
            })

        # ---- nutrients (per 100 g edible part) ------------------------------
        values = raw_values(f)
        for nid, options in MVT_MAPPING:
            hit = resolve(values, options, unit_warnings)
            if hit is None:
                continue                       # NULL - honest gap
            amount, _code, sid = hit
            food_nutrients.append({
                "id": next_fn_id,
                "food_id": food_id,
                "nutrient_id": nid,
                "amount": amount,
                "origin": origin_for(sid, source_desc.get(sid, "")),
                "reference": sid,
                "data_points": "", "min": "", "max": "",
            })
            next_fn_id += 1

        # ---- household measures -> food_portion ----------------------------
        usable = []
        for p in f.get("portions") or []:
            grams = p.get("quantity")
            label = str(p.get("portionName") or "").strip()
            if (str(p.get("unit") or "").strip() != "g" or not label
                    or not isinstance(grams, (int, float)) or grams <= 0):
                skipped_portions += 1
                continue
            usable.append((portion_rank(str(p.get("id") or "")), label,
                           float(grams)))
        for seq, (_rank, label, grams) in enumerate(sorted(usable), start=1):
            portions.append({
                "id": PORTION_ID_BASE + len(portions),
                "food_id": food_id,
                "amount": 1,
                "measure_unit_id": UNDETERMINED_UNIT_ID,
                "portion_description": f"1 {label}",
                "modifier": "",
                "seq_num": seq,
                "gram_weight": round(grams, 2),
            })

    for w in sorted(unit_warnings):
        print(f"  WARNING: unexpected unit, value skipped -> {w}",
              file=sys.stderr)
    if unknown_groups:
        print(f"  note: foods reference unknown food groups "
              f"{sorted(unknown_groups)} - category left empty",
              file=sys.stderr)
    if skipped_portions:
        print(f"  note: {skipped_portions} portions skipped (not grams / "
              f"no name / no weight)", file=sys.stderr)

    # ------------------------------------------------------------------ write
    def write(name: str, rows_, columns: list[str]) -> None:
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_)
        print(f"  {name:20s} {len(rows_):>7,} rows")

    print("Writing Supabase CSVs:")
    write("food_source",
          [{"code": "mvt",
            "name": f"Matvaretabellen {EDITION} (Norwegian Food Composition "
                    f"Table, Mattilsynet)",
            "license_note": f"CC BY 4.0 - attribute \"Matvaretabellen "
                            f"{EDITION}. Mattilsynet. www.matvaretabellen.no\""}],
          ["code", "name", "license_note"])
    write("nutrient_mapping",
          [{"source": "mvt", "source_component_code": c,
            "nutrient_id": nid, "unit_factor": fac}
           for nid, opts in MVT_MAPPING for c, _u, fac in opts],
          ["source", "source_component_code", "nutrient_id", "unit_factor"])
    write("food_category", categories, ["id", "source", "description"])
    write("food", foods,
          ["id", "source", "source_code", "description", "short_title",
           "food_category_id", "publication_date"])
    write("food_translation", translations,
          ["food_id", "locale", "description", "source"])
    write("food_portion", portions,
          ["id", "food_id", "amount", "measure_unit_id",
           "portion_description", "modifier", "seq_num", "gram_weight"])
    write("food_nutrient", food_nutrients,
          ["id", "food_id", "nutrient_id", "amount", "origin",
           "reference", "data_points", "min", "max"])

    n_energy = len({r["food_id"] for r in food_nutrients if r["nutrient_id"] == 1})
    n_portioned = len({p["food_id"] for p in portions})
    print(f"\nDone. {len(foods):,} MVT foods ({n_energy:,} with energy, "
          f"{len(portions):,} portions on {n_portioned:,} foods, "
          f"{len(categories)} categories) -> {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert Matvaretabellen JSON to multi-source Supabase CSVs")
    ap.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR,
                    help="folder from download_mvt.py holding the JSON files "
                         "(default: ./source/mvt)")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    if not args.source_dir.is_dir():
        sys.exit(f"error: {args.source_dir} not found - run "
                 f"download_mvt.py --output source first")
    print(f"source: {args.source_dir}")
    convert(args.source_dir, args.output)


if __name__ == "__main__":
    main()
