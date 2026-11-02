#!/usr/bin/env python3
"""Convert the Fineli open-data package (Finnish food composition
database, THL) into CSV files matching the multi-source Supabase schema
(multisource_food_schema.mermaid) - the same tables the BLS and INDB
converters fill.

Emitted CSVs:

    food_source.csv       (1 row: fineli)
    nutrient_mapping.csv  (source='fineli' rows, every unit factor)
    food_category.csv     (Fineli food-use classes FUCLASS, ids 50001+,
                           source='fineli', English names)
    food.csv              (ids start at 50_000_000; English names)
    food_translation.csv  (native Finnish 'fi' and Swedish 'sv' names)
    food_alias.csv        (scientific names, locale 'la', source 'derived')
    food_portion.csv      (household measures from foodaddunit.csv:
                           pieces, portions, dl, tbsp, tsp - in grams)
    food_nutrient.csv     (canonical nutrients, units converted)

NOT emitted: nutrient.csv (shared canonical list, ships with the FDC
conversion) and measure_unit.csv (portions reference FDC's unit 9999
'undetermined' and carry the household measure in portion_description,
exactly like INDB; load the FDC CSVs first).

Usage:
    # with the CSVs auto-found under ./source/fineli (download_fineli.py):
    python fineli_to_ont_csv.py --output <out_dir>

    # or point at the extracted package folder explicitly:
    python fineli_to_ont_csv.py --source-dir path/to/package --output <out_dir>

Typical pipeline:
    python download_fineli.py --output source        # -> ./source/fineli
    python fineli_to_ont_csv.py --output ../../out/fineli

Standard library only.

Import (append) after FDC:
    \\copy food_source from 'food_source.csv' csv header
    \\copy nutrient_mapping from 'nutrient_mapping.csv' csv header
    \\copy food_category from 'food_category.csv' csv header
    \\copy food from 'food.csv' csv header
    \\copy food_translation (food_id, locale, description, source) from 'food_translation.csv' csv header
    \\copy food_nutrient from 'food_nutrient.csv' csv header
    \\copy food_portion from 'food_portion.csv' csv header
    \\copy food_alias (food_id, locale, alias, source) from 'food_alias.csv' csv header
    refresh materialized view food_summary;

Notes
-----
* id ranges: food 50_000_000+, food_nutrient 60_000_000+, food_portion
  50_000_000+, food_category 50_001+ - clear of FDC/BLS/INDB/TBCA.
* Names: Fineli ships every name in UPPER CASE (all three languages).
  They are sentence-cased on import ("RYE BREAD, WHOLEGRAIN" ->
  "Rye bread, wholegrain"), as fineli.fi itself displays them. Vitamin
  letters keep their capital ("vitamin D", "D-vitamiinia"); brand names
  and other acronyms do not ("Fazer" -> "fazer", "UHT" -> "uht").
* Unit conversions (documented row by row in nutrient_mapping):
    - energy: the package carries only ENERC in kJ (no kcal component),
      so kcal = kJ / 4.184 (unit_factor 0.2390057...), the factor
      fineli.fi uses for its kcal column.
    - everything else is already in the canonical unit (g / mg / ug).
* Nutrient choices:
    - carbohydrates = CHOAVL (available carbohydrate, like BLS CHO), not
      CHOCDF (by difference, which includes fibre).
    - fibre = FIBC (Fineli's "fibre, total"), FIBT as fallback.
    - monounsaturated = FAMCIS (cis-MUFA; trans isomers are in FATRN).
    - trans fat = FATRN - Fineli HAS it (unlike BLS/INDB).
    - cholesterol = CHOLE (GC method).
    - vitamin A = VITA, already ug retinol activity equivalents (RAE) -
      the canonical unit; XVITA (retinol equivalents) is not used.
    - vitamin D = VITD (ug), vitamin B12 = VITB12 (ug).
    - vitamin B6 = VITPYRID (mg, pyridoxine vitamers as hydrochloride) -
      what Fineli publishes as vitamin B6.
    - niacin = NIA (preformed niacin, mg), not NIAEQ (which adds the
      tryptophan contribution) - same concept as FDC 1167 / BLS NIA.
* Nullable = honest: a component with no row (or an empty value) in
  component_value.csv stays NULL; a published 0 stays 0. Six foods (some
  spices, boiled water) ship with no values at all and are kept with no
  nutrients.
* origin: ACQTYPE L (label) -> label; METHTYPE A/AG/D (analytical) ->
  analysis; ACQTYPE F/P/C/A (tables, papers) -> literature; any other
  known method (recipe, summation, imputed...) -> calculated.
* Portions: one food_portion per foodaddunit row except KG/L (pure scale
  factors with no household meaning). The default serving (seq_num 1) is
  the medium piece if the food has one, else the medium portion.
  portion_description is "1 <English unit name from foodunit_EN.csv>".
* Encoding: the official zips are Latin-1; UTF-8 copies are accepted too
  (each file is decoded as UTF-8 first, then cp1252).
* Fineli is open data under CC BY 4.0 - attribute the Finnish Institute
  for Health and Welfare (THL), Fineli, release from descript.txt.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

FOOD_ID_BASE = 50_000_000
NUTRIENT_ID_BASE = 60_000_000
PORTION_ID_BASE = 50_000_000
CATEGORY_ID_BASE = 50_000
UNDETERMINED_UNIT_ID = 9999          # FDC measure_unit 'undetermined'
DEFAULT_SOURCE_DIR = Path("source") / "fineli"  # what download_fineli.py fills

KJ_TO_KCAL = 1 / 4.184

# ---------------------------------------------------------------------------
# Canonical nutrient ids (must match fdc_to_ont_csv.py) mapped to Fineli
# EUFDNAME codes, in priority order (first present per food wins).
# unit_factor converts the Fineli unit to the canonical unit.
# ---------------------------------------------------------------------------
FINELI_MAPPING = [
    # (canonical id, [(eufdname, unit_factor), ...])
    (1,  [("ENERC", KJ_TO_KCAL)]),    # kJ -> kcal (/4.184)
    (2,  [("CHOAVL", 1)]),            # available carbohydrate, g
    (3,  [("FAT", 1)]),               # g
    (4,  [("PROT", 1)]),              # g
    (5,  [("SUGAR", 1)]),             # total sugars, g
    (6,  [("FASAT", 1)]),             # g
    (7,  [("FIBC", 1), ("FIBT", 1)]), # total fibre, g
    (8,  [("FAMCIS", 1)]),            # cis-MUFA, g
    (9,  [("FAPU", 1)]),              # g
    (10, [("FATRN", 1)]),             # trans fatty acids, g
    (11, [("CHOLE", 1)]),             # mg
    (12, [("NA", 1)]),                # mg
    (13, [("K", 1)]),                 # mg
    (14, [("MG", 1)]),                # mg
    (15, [("CA", 1)]),                # mg
    (16, [("FE", 1)]),                # mg
    (17, [("ZN", 1)]),                # mg
    (18, [("P", 1)]),                 # mg
    (19, [("VITA", 1)]),              # ug RAE
    (20, [("VITC", 1)]),              # mg
    (21, [("VITD", 1)]),              # ug
    (22, [("VITPYRID", 1)]),          # mg
    (23, [("VITB12", 1)]),            # ug
    (24, [("NIA", 1)]),               # preformed niacin, mg
]

# foodaddunit FOODUNIT codes -> default-serving preference (lower first).
# KG and L are skipped: pure scale factors, not household measures.
PORTION_ORDER = ["KPL_M", "PORTM", "KPL_VALM", "KPL_S", "KPL_L",
                 "PORTS", "PORTL", "DL", "RKL", "TL"]
SKIP_UNITS = {"KG", "L"}
# English label override where foodunit_EN.csv reads poorly as a serving.
UNIT_LABEL_OVERRIDE = {"KPL_VALM": "piece (manufacturer's size)"}

ANALYTICAL_METHODS = {"A", "AG", "D"}
LITERATURE_ACQ = {"F", "P", "C", "A"}


# vitamin letters keep their capital: 'vitamin d' / 'd-vitamiinia'
VITAMIN_LETTER_RE = re.compile(r"(?<=\bvitamin )([a-z])\b|\b([a-z])(?=-vitamiin)")


def sentence_case(name: str) -> str:
    """'RYE BREAD, WHOLEGRAIN' -> 'Rye bread, wholegrain' (Fineli ships
    names in upper case). Vitamin letters stay upper case."""
    s = " ".join(name.split())
    s = s[:1].upper() + s[1:].lower()
    return VITAMIN_LETTER_RE.sub(lambda m: m.group(0).upper(), s)


def short_title(description: str) -> str:
    """Concise display name: text before the first comma, capped at 80."""
    head = description.split(",")[0].strip()
    return (head or description.strip())[:80]


def parse_amount(value: str | None) -> float | None:
    """Fineli values use a decimal comma ('1698,30'); empty = missing."""
    s = (value or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def origin_of(acqtype: str, methtype: str) -> str:
    acq, meth = acqtype.strip().upper(), methtype.strip().upper()
    if acq == "L":
        return "label"
    if meth in ANALYTICAL_METHODS:
        return "analysis"
    if acq in LITERATURE_ACQ:
        return "literature"
    if meth and meth != "X":
        return "calculated"
    return ""


def read_table(source_dir: Path, name: str, required: bool = True) -> list[dict]:
    """Read one semicolon-separated Fineli file (case-insensitive name),
    decoding UTF-8 first and cp1252 (Latin-1 superset) as fallback."""
    hits = [p for p in source_dir.iterdir() if p.name.lower() == name.lower()]
    if not hits:
        if required:
            sys.exit(f"error: {name} not found under {source_dir} - run "
                     f"download_fineli.py first, or pass --source-dir")
        return []
    raw = hits[0].read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", "replace")
    rows = csv.DictReader(io.StringIO(text, newline=""), delimiter=";")
    return [{(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}
            for r in rows]


def names_by_food(source_dir: Path, lang: str) -> dict[str, str]:
    return {r["FOODID"]: r["FOODNAME"]
            for r in read_table(source_dir, f"foodname_{lang}.csv",
                                required=(lang == "EN"))
            if r.get("FOODID") and r.get("FOODNAME")}


def classification(source_dir: Path, name: str) -> dict[str, str]:
    return {r["THSCODE"]: r["DESCRIPT"]
            for r in read_table(source_dir, name, required=False)
            if r.get("THSCODE")}


def load_nutrients(source_dir: Path) -> dict[str, dict[int, tuple]]:
    """{FOODID: {canonical_id: (amount, eufdname, origin)}} after the
    priority + unit-factor mapping. Shared with test_against_source.py."""
    by_code: dict[str, list[tuple[int, int, float]]] = {}
    for nid, options in FINELI_MAPPING:
        for prio, (code, factor) in enumerate(options):
            by_code.setdefault(code, []).append((nid, prio, factor))
    best: dict[str, dict[int, tuple]] = {}
    for r in read_table(source_dir, "component_value.csv"):
        targets = by_code.get(r.get("EUFDNAME", ""))
        if not targets:
            continue
        amount = parse_amount(r.get("BESTLOC"))
        if amount is None:
            continue
        per_food = best.setdefault(r["FOODID"], {})
        for nid, prio, factor in targets:
            cur = per_food.get(nid)
            if cur is None or prio < cur[0]:
                per_food[nid] = (prio, round(amount * factor, 6), r["EUFDNAME"],
                                 origin_of(r.get("ACQTYPE", ""),
                                           r.get("METHTYPE", "")))
    return {fid: {nid: v[1:] for nid, v in d.items()} for fid, d in best.items()}


def release_of(source_dir: Path) -> str:
    for p in source_dir.iterdir():
        if p.name.lower() == "descript.txt":
            m = re.search(rb"Release\.?\s+([\d.]+)", p.read_bytes())
            if m:
                return m.group(1).decode()
    return ""


def convert(source_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    food_rows = read_table(source_dir, "food.csv")
    names_en = names_by_food(source_dir, "EN")
    names_fi = names_by_food(source_dir, "FI")
    names_sv = names_by_food(source_dir, "SV")
    names_tx = names_by_food(source_dir, "TX")
    fuclass = classification(source_dir, "fuclass_EN.csv")
    units_en = classification(source_dir, "foodunit_EN.csv")
    nutrients = load_nutrients(source_dir)
    release = release_of(source_dir)

    portions_by_food: dict[str, list[tuple[str, float]]] = {}
    unknown_units: set[str] = set()
    for r in read_table(source_dir, "foodaddunit.csv", required=False):
        unit, grams = r.get("FOODUNIT", ""), parse_amount(r.get("MASS"))
        if unit in SKIP_UNITS or not grams or grams <= 0:
            continue
        if unit not in PORTION_ORDER:
            unknown_units.add(unit)
        portions_by_food.setdefault(r["FOODID"], []).append((unit, grams))

    foods, translations, aliases, portions, food_nutrients = [], [], [], [], []
    categories: dict[str, int] = {}
    seen: set[str] = set()
    next_fn_id = NUTRIENT_ID_BASE

    for row in food_rows:
        fid = row.get("FOODID", "")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        raw_en = names_en.get(fid) or row.get("FOODNAME", "")
        if not raw_en:
            continue
        description = sentence_case(raw_en)
        food_id = FOOD_ID_BASE + len(foods)

        cat_id = ""
        fu = row.get("FUCLASS", "")
        if fu and fu in fuclass:
            if fu not in categories:
                categories[fu] = CATEGORY_ID_BASE + len(categories) + 1
            cat_id = categories[fu]

        foods.append({
            "id": food_id,
            "source": "fineli",
            "source_code": fid,
            "description": description,
            "short_title": short_title(description),
            "food_category_id": cat_id,
            "publication_date": "",
        })
        for locale, names in (("fi", names_fi), ("sv", names_sv)):
            if names.get(fid):
                translations.append({
                    "food_id": food_id,
                    "locale": locale,
                    "description": sentence_case(names[fid]),
                    "source": "native",
                })
        if names_tx.get(fid):
            aliases.append({
                "food_id": food_id,
                "locale": "la",           # Latin binomial, searchable
                "alias": sentence_case(names_tx[fid]),
                "source": "derived",
            })

        for nid, (amount, _code, origin) in sorted(nutrients.get(fid, {}).items()):
            food_nutrients.append({
                "id": next_fn_id,
                "food_id": food_id,
                "nutrient_id": nid,
                "amount": amount,
                "origin": origin,
                "reference": "",
                "data_points": "", "min": "", "max": "",
            })
            next_fn_id += 1

        ranked = sorted(portions_by_food.get(fid, []),
                        key=lambda ug: (PORTION_ORDER.index(ug[0])
                                        if ug[0] in PORTION_ORDER
                                        else len(PORTION_ORDER), ug[0]))
        for seq, (unit, grams) in enumerate(ranked, start=1):
            label = UNIT_LABEL_OVERRIDE.get(unit) or units_en.get(unit) or unit
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

    if unknown_units:
        print(f"  note: foodaddunit units not in PORTION_ORDER (ranked last): "
              f"{sorted(unknown_units)}", file=sys.stderr)

    # ------------------------------------------------------------------ write
    def write(name: str, rows_, columns: list[str]) -> None:
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_)
        print(f"  {name:20s} {len(rows_):>7,} rows")

    rel = f" release {release}" if release else ""
    print("Writing Supabase CSVs:")
    write("food_source",
          [{"code": "fineli",
            "name": f"Fineli{rel} - Finnish food composition database (THL)",
            "license_note": "CC BY 4.0 - attribute Finnish Institute for "
                            f"Health and Welfare (THL), Fineli{rel}, "
                            "https://fineli.fi"}],
          ["code", "name", "license_note"])
    write("nutrient_mapping",
          [{"source": "fineli", "source_component_code": c,
            "nutrient_id": nid, "unit_factor": f}
           for nid, opts in FINELI_MAPPING for c, f in opts],
          ["source", "source_component_code", "nutrient_id", "unit_factor"])
    write("food_category",
          [{"id": cid, "source": "fineli", "description": fuclass[code]}
           for code, cid in sorted(categories.items(), key=lambda kv: kv[1])],
          ["id", "source", "description"])
    write("food", foods,
          ["id", "source", "source_code", "description", "short_title",
           "food_category_id", "publication_date"])
    write("food_translation", translations,
          ["food_id", "locale", "description", "source"])
    write("food_alias", aliases, ["food_id", "locale", "alias", "source"])
    write("food_portion", portions,
          ["id", "food_id", "amount", "measure_unit_id",
           "portion_description", "modifier", "seq_num", "gram_weight"])
    write("food_nutrient", food_nutrients,
          ["id", "food_id", "nutrient_id", "amount", "origin",
           "reference", "data_points", "min", "max"])

    n_energy = len({r["food_id"] for r in food_nutrients if r["nutrient_id"] == 1})
    n_portioned = len({p["food_id"] for p in portions})
    print(f"\nDone. {len(foods):,} Fineli foods ({n_energy:,} with energy, "
          f"{n_portioned:,} with portions, {len(portions):,} portions, "
          f"{len(categories)} categories) -> {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert the Fineli open-data CSVs to multi-source "
                    "Supabase CSVs")
    ap.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR,
                    help="folder holding the extracted Fineli CSVs "
                         "(default: ./source/fineli)")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    if not args.source_dir.is_dir():
        sys.exit(f"error: {args.source_dir} not found - run "
                 f"download_fineli.py first, or pass --source-dir")
    print(f"source: {args.source_dir}")
    convert(args.source_dir, args.output)


if __name__ == "__main__":
    main()
