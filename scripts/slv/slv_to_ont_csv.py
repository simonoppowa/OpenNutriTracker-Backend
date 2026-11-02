#!/usr/bin/env python3
"""Convert the Swedish Food Agency's food composition database
(Livsmedelsverket, "Livsmedelsdatabasen"), as cached by download_slv.py,
into CSV files matching the multi-source Supabase schema
(multisource_food_schema.mermaid) - the same tables bls_to_ont_csv.py
fills for BLS.

Emitted CSVs:

    food_source.csv       (1 row: slv)
    nutrient_mapping.csv  (source='slv' rows only)
    food_category.csv     (SLV "Main group", ids 70001+, source='slv')
    food.csv              (ids start at 70_000_000; English names)
    food_translation.csv  (native Swedish names, locale 'sv')
    food_nutrient.csv     (canonical nutrients, units converted)

NOT emitted: nutrient.csv - the canonical nutrient list is shared and must
already be loaded (it ships with the FDC conversion). No food_portion /
food_image either: the Livsmedelsverket API carries neither serving sizes
nor photos. No per-source side table: every SLV component used is
documented in nutrient_mapping.

Usage:
    # with the JSON auto-found under ./source/slv (download_slv.py):
    python slv_to_ont_csv.py --output <out_dir>

    # or point at the cache explicitly:
    python slv_to_ont_csv.py --source-dir /data/slv --output <out_dir>

Typical pipeline:
    python download_slv.py --output source           # -> ./source/slv
    python slv_to_ont_csv.py --output ../../out/slv  # reads ./source/slv
    python shared/import_fdc.py --csv-dir out/slv --append   # after FDC

Standard library only.

Notes
-----
* id ranges: food.id = 70_000_000 + n, food_nutrient.id = 80_000_000 + n,
  food_category.id = 70_001+ - clear of FDC/BLS/INDB/TBCA.
* food.description is the API's English name (sprak=2, British English).
  Every food in the current release has one; if a future release lacks
  it, the Swedish name is used instead ("SV fallback", like BLS's DE
  fallback). The Swedish name (sprak=1) always becomes a native
  food_translation row (locale 'sv').
* Energy: SLV ships ENERC twice, in kJ and in kcal (its kcal is
  kJ x 0.239). The kcal row is used directly; if it is ever missing, the
  kJ row is converted with SLV's own factor 0.239. In nutrient_mapping the
  two rows are told apart as 'ENERC:kcal' / 'ENERC:kJ'.
* Vitamin A: SLV's VITA is labelled "Retinol equivalents" (unit RE/ug),
  but its formula (the API's `berakning` field) is
  retinol + beta-carotene/12 + other provitamin-A carotenoids/24 - the
  retinol ACTIVITY equivalent (NNR 2023 definition). It therefore maps 1:1
  onto canonical vitamin A (ug RAE), factor 1.
* Niacin is preformed niacin (NIA, mg), not niacin equivalents (NIAEQ).
  Vitamin D is VITD (D2 + D3); VITD_x ("incl. 25-OH-D3") is not used.
  Carbohydrates = CHO (available carbohydrate); sugars = SUGAR (total
  sugars, NOT added sugar SUGAD). Every other unit already matches the
  canonical one (g / mg / ug per 100 g edible portion) - factor 1.
* Trans fat: SLV reports no trans-fatty-acid component -> trans_fat_100
  stays NULL for every SLV food.
* A row whose value is missing stays NULL. Rows SLV marks "Logical zero",
  "Below limit of detection" or "Trace" carry the value SLV publishes
  (usually 0) - that is the source's statement, not a gap.
* origin: food label (ursprungkod L) -> label; analytical method types
  (A, AG, D) -> analysis; values taken from another food composition
  table or a scientific communication (ursprungkod F, C) -> literature;
  calculated / imputed / deduced method types -> calculated; unknown ->
  empty. reference = SLV's `publikation`.
* License: CC BY 4.0, Livsmedelsverket - "you have permission to use and
  distribute our open data and create processing. The Swedish Food
  Agency must be stated as the source."
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

FOOD_ID_BASE = 70_000_000          # clear of FDC/BLS/INDB/TBCA
NUTRIENT_ID_BASE = 80_000_000      # food_nutrient ids
CATEGORY_ID_BASE = 70_000          # food_category ids 70001+
DEFAULT_SOURCE_DIR = Path("source") / "slv"  # what download_slv.py fills
KJ_TO_KCAL = 0.239                 # SLV's own factor (kcal = kJ * 0.239)

# ---------------------------------------------------------------------------
# Canonical nutrient ids (must match fdc_to_ont_csv.py) mapped to SLV
# components, in priority order (first present per food wins). A component
# is (EuroFIR code, SLV unit); the unit is part of the key because ENERC is
# published twice (kJ and kcal) and so that a unit change in a future
# release is skipped loudly instead of imported at the wrong scale.
# unit_factor converts the SLV unit to the canonical unit.
# ---------------------------------------------------------------------------
SLV_MAPPING = [
    # (canonical id, [(code, unit, unit_factor), ...])
    (1,  [("ENERC", "kcal", 1), ("ENERC", "kJ", KJ_TO_KCAL)]),  # kcal
    (2,  [("CHO", "g", 1)]),          # available carbohydrate
    (3,  [("FAT", "g", 1)]),
    (4,  [("PROT", "g", 1)]),
    (5,  [("SUGAR", "g", 1)]),        # total sugars (not SUGAD added)
    (6,  [("FASAT", "g", 1)]),
    (7,  [("FIBT", "g", 1)]),
    (8,  [("FAMS", "g", 1)]),
    (9,  [("FAPU", "g", 1)]),
    # 10: trans fat - no SLV component, stays NULL
    (11, [("CHORL", "mg", 1)]),
    (12, [("NA", "mg", 1)]),
    (13, [("K", "mg", 1)]),
    (14, [("MG", "mg", 1)]),
    (15, [("CA", "mg", 1)]),
    (16, [("FE", "mg", 1)]),
    (17, [("ZN", "mg", 1)]),
    (18, [("P", "mg", 1)]),
    (19, [("VITA", "RE/µg", 1)]),     # = RAE (retinol + b-carotene/12 ...)
    (20, [("VITC", "mg", 1)]),
    (21, [("VITD", "µg", 1)]),
    (22, [("VITB6", "mg", 1)]),
    (23, [("VITB12", "µg", 1)]),
    (24, [("NIA", "mg", 1)]),         # preformed niacin, not NIAEQ
]

# EuroFIR acquisition type (ursprungkod) / method type (metodtypkod)
# -> food_nutrient.origin
LABEL_ACQUISITION = {"L"}                       # food label, product info
LITERATURE_ACQUISITION = {"F", "C"}             # other FCDB, sci. comm.
ANALYSIS_METHODS = {"A", "AG", "D"}             # analytical (incl. aggregated)
CALCULATED_METHODS = {"T", "R", "S", "CG", "G", "K", "U", "I", "IG"}


def mapping_code(code: str, unit: str) -> str:
    """nutrient_mapping.source_component_code: the EuroFIR code, plus the
    unit for ENERC (published in both kJ and kcal)."""
    return f"{code}:{unit}" if code == "ENERC" else code


def short_title(description: str) -> str:
    """Concise display name: text before the first comma, capped at 80."""
    head = description.split(",")[0].strip()
    return (head or description.strip())[:80]


def parse_amount(value) -> float | None:
    """API values are JSON numbers; tolerate comma-decimal strings."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def origin_of(row: dict) -> str:
    acq = (row.get("ursprungkod") or "").strip()
    method = (row.get("metodtypkod") or "").strip()
    if acq in LABEL_ACQUISITION:
        return "label"
    if method in ANALYSIS_METHODS:
        return "analysis"
    if acq in LITERATURE_ACQUISITION:
        return "literature"
    if method in CALCULATED_METHODS:
        return "calculated"
    return ""


def index_components(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """(EuroFIR code, unit) -> raw naringsvarden row (first one wins)."""
    out = {}
    for r in rows:
        key = ((r.get("euroFIRkod") or "").strip(), (r.get("enhet") or "").strip())
        out.setdefault(key, r)
    return out


def resolve_nutrients(rows: list[dict]) -> dict[int, tuple[float, dict]]:
    """canonical id -> (amount in canonical unit, raw row) for one food,
    applying SLV_MAPPING priority + unit factors."""
    comps = index_components(rows)
    out = {}
    for nid, options in SLV_MAPPING:
        for code, unit, factor in options:
            r = comps.get((code, unit))
            amount = parse_amount(r.get("varde")) if r else None
            if amount is not None and amount >= 0:
                out[nid] = (round(amount * factor, 6), r)
                break
    return out


def main_group(classifications: list[dict]) -> str:
    for c in classifications or []:
        if (c.get("typ") or "").strip().lower() == "main group":
            return (c.get("kod") or "").strip()
    return ""


def load_json(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        sys.exit(f"error: {path} is not valid JSON ({e}) - delete it and "
                 f"re-run download_slv.py")


def convert(source_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    en = load_json(source_dir / "livsmedel_en.json")
    sv = load_json(source_dir / "livsmedel_sv.json")
    if en is None or sv is None:
        sys.exit(f"error: livsmedel_en.json / livsmedel_sv.json not found in "
                 f"{source_dir} - run download_slv.py first, or pass "
                 f"--source-dir")
    sv_name = {f["nummer"]: (f.get("namn") or "").strip() for f in sv}

    # warn about units the mapping does not expect (unit discipline)
    expected = {(c, u) for _, opts in SLV_MAPPING for c, u, _ in opts}
    mapped_codes = {c for c, _ in expected}
    odd_units: set[tuple[str, str]] = set()

    categories: dict[str, int] = {}
    foods, translations, food_nutrients = [], [], []
    seen: set[int] = set()
    missing_nut: list[int] = []
    sv_fallback = 0
    next_fn_id = NUTRIENT_ID_BASE

    for f in en:
        nummer = f.get("nummer")
        if nummer is None or nummer in seen:
            continue
        seen.add(nummer)
        name_en = (f.get("namn") or "").strip()
        name_sv = sv_name.get(nummer, "")
        if not (name_en or name_sv):
            continue
        if not name_en:
            sv_fallback += 1
        description = name_en or name_sv          # English; SV fallback

        nut_rows = load_json(source_dir / "naringsvarden" / f"{nummer}.json")
        if nut_rows is None:
            missing_nut.append(nummer)
            nut_rows = []
        group = main_group(load_json(
            source_dir / "klassificeringar" / f"{nummer}.json") or [])
        cat_id = ""
        if group:
            if group not in categories:
                categories[group] = CATEGORY_ID_BASE + len(categories) + 1
            cat_id = categories[group]

        food_id = FOOD_ID_BASE + len(foods)
        foods.append({
            "id": food_id,
            "source": "slv",
            "source_code": str(nummer),
            "description": description,
            "short_title": short_title(description),
            "food_category_id": cat_id,
            "publication_date": (f.get("version") or "")[:10],
        })
        if name_sv:
            translations.append({
                "food_id": food_id,
                "locale": "sv",
                "description": name_sv,
                "source": "native",
            })

        for r in nut_rows:
            key = ((r.get("euroFIRkod") or "").strip(),
                   (r.get("enhet") or "").strip())
            if key[0] in mapped_codes and key not in expected:
                odd_units.add(key)
        for nid, (amount, r) in sorted(resolve_nutrients(nut_rows).items()):
            food_nutrients.append({
                "id": next_fn_id,
                "food_id": food_id,
                "nutrient_id": nid,
                "amount": amount,
                "origin": origin_of(r),
                "reference": (r.get("publikation") or "").strip(),
                "data_points": "",
                "min": "",
                "max": "",
            })
            next_fn_id += 1

    if missing_nut:
        sys.exit(f"error: {len(missing_nut)} food(s) have no cached nutrient "
                 f"file (e.g. naringsvarden/{missing_nut[0]}.json) - re-run "
                 f"download_slv.py to fetch the missing ones")
    if odd_units:
        print(f"  note: skipped mapped components with an unexpected unit: "
              f"{sorted(odd_units)}", file=sys.stderr)
    if sv_fallback:
        print(f"  note: {sv_fallback} foods have no English name - Swedish "
              f"used as description", file=sys.stderr)

    # ------------------------------------------------------------------ write
    def write(name: str, rows_, columns: list[str]) -> None:
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_)
        print(f"  {name:20s} {len(rows_):>7,} rows")

    info = load_json(source_dir / "api_info.json") or {}
    print("Writing Supabase CSVs:")
    write("food_source",
          [{"code": "slv",
            "name": "Swedish Food Composition Database (Livsmedelsverket, "
                    "Livsmedelsdatabasen)",
            "license_note": "CC BY 4.0 - source: Swedish Food Agency "
                            "(Livsmedelsverket), Livsmedelsdatabasen; "
                            "restructured into this schema (values "
                            "unchanged unless unit-converted)"}],
          ["code", "name", "license_note"])
    write("nutrient_mapping",
          [{"source": "slv", "source_component_code": mapping_code(c, u),
            "nutrient_id": nid, "unit_factor": fac}
           for nid, opts in SLV_MAPPING for c, u, fac in opts],
          ["source", "source_component_code", "nutrient_id", "unit_factor"])
    write("food_category",
          [{"id": cid, "source": "slv", "description": g}
           for g, cid in sorted(categories.items(), key=lambda kv: kv[1])],
          ["id", "source", "description"])
    write("food", foods,
          ["id", "source", "source_code", "description", "short_title",
           "food_category_id", "publication_date"])
    write("food_translation", translations,
          ["food_id", "locale", "description", "source"])
    write("food_nutrient", food_nutrients,
          ["id", "food_id", "nutrient_id", "amount", "origin",
           "reference", "data_points", "min", "max"])

    n_energy = len({r["food_id"] for r in food_nutrients if r["nutrient_id"] == 1})
    print(f"\nDone. {len(foods):,} SLV foods ({n_energy:,} with energy, "
          f"{len(categories)} categories; API release "
          f"{info.get('apiReleased', '?')}) -> {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert the Livsmedelsverket food database (cached API "
                    "JSON) to multi-source Supabase CSVs")
    ap.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR,
                    help="folder filled by download_slv.py "
                         "(default: ./source/slv)")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    if not args.source_dir.is_dir():
        sys.exit(f"error: {args.source_dir} not found - run download_slv.py "
                 f"first, or pass --source-dir")
    print(f"source: {args.source_dir}")
    convert(args.source_dir, args.output)


if __name__ == "__main__":
    main()
