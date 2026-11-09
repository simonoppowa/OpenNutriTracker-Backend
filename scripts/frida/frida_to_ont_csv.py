#!/usr/bin/env python3
"""Convert the Danish Food Composition Database ("Frida", DTU National
Food Institute) dataset workbook into CSV files matching the multi-source
Supabase schema (multisource_food_schema.mermaid) - the same tables
bls_to_ont_csv.py fills for BLS.

Emitted CSVs:

    food_source.csv       (1 row: frida, with the release's citation)
    nutrient_mapping.csv  (source='frida' rows; codes are Frida ParameterIDs)
    food_category.csv     (Frida's leaf food groups, ids 60_000 + FoodGroupID)
    food.csv              (ids 60_000_000 + FoodID; English names)
    food_translation.csv  (native Danish names, locale 'da')
    food_nutrient.csv     (canonical nutrients, ids from 70_000_000)

NOT emitted: nutrient.csv (shared canonical list, ships with the FDC
conversion), no per-source component catalog (that would need a new
table), and no food_portion: the CC BY dataset has no portion sizes (the
website shows some, but they are not part of the published dataset).
No food_image either.

Usage:
    # with the workbook auto-found under ./source/frida (download_frida.py):
    python frida_to_ont_csv.py --output <out_dir>

    # or point at it explicitly:
    python frida_to_ont_csv.py --data FCDB_6.1_Dataset.xlsx --output <out_dir>

Typical pipeline:
    python download_frida.py --output source          # -> ./source/frida
    python frida_to_ont_csv.py --output ../../out/frida

Requires: pip install openpyxl

Import after the FDC CSVs (FDC ships the shared `nutrient` table); append:
    python ../shared/import_fdc.py --csv-dir ../../out/frida --append

Notes
-----
* Values are read from the long 'Data_Normalised' sheet (one row per food
  x parameter, with source ids, min/max and number of determinations);
  test_against_source.py re-derives them from the wide 'Data_Table' sheet.
* food.id = 60_000_000 + Frida FoodID, so ids stay stable across releases.
* Energy: 'Energy (kcal)' (ParameterID 356), already in kcal, factor 1 -
  EU 1169/2011 factors applied to Frida's scientific protein (N x Jones
  factor, 218) and available carbohydrate (172), the same two values this
  converter maps, so energy and macros agree. The kJ value (137) is a
  fallback only, x 1/4.184 (never needed in v6.1: every food has kcal).
  The 'labelling' variants (316/359, protein N x 6.25 = 317, carbohydrate
  sugars + starch = 318) are not used.
* Carbohydrates = 'Available carbohydrates' (172; total carbohydrate minus
  dietary fibre), as BLS CHO. Fibre is mapped separately (168).
* Sugars = 'Sum sugars' (245; mono + disaccharides) - NOT 'Added Sugar'
  (417) or 'Free Sugars' (418).
* Trans fat = 'Sum trans fatty acids' (261) - Frida has it, unlike BLS.
* Vitamin A: Frida publishes retinol equivalents, RE = retinol (225) +
  beta-carotene (303) / 6 (ParameterID 12; documentation sect. 6.1).
  Canonical is ug RAE = retinol + beta-carotene / 12, derived exactly from
  any two of the three:
      retinol + beta-carotene / 12      (both measured)
      (RE + retinol) / 2                (beta-carotene missing)
      RE - beta-carotene / 12           (retinol missing)
  RE alone (35 foods in v6.1) is left NULL rather than passed off as RAE.
* Vitamin D = Frida 'Vitamin D' (126) = D2 + D3 + 2.5 x (25-OH-D2 +
  25-OH-D3) (EFSA weighting, since v5.1), ug, factor 1.
* Niacin = preformed 'Niacin' (294), mg - NOT 'Niacin equivalent' (203,
  which adds tryptophan / 60), matching FDC 1167 and BLS NIA.
* Every other mapped parameter is already in the canonical unit (g, mg or
  ug per 100 g; checked against the Parameter sheet at run time).
  Nothing is a gap in v6.1: all 24 canonical nutrients have a Frida
  parameter; a missing value for a food stays NULL.
* origin: Frida SourceIDs of type 'E' (calculated, estimated, recipe,
  natural zero) -> calculated; 'L' (label / product information) ->
  label; any other single cited source (reports, articles, databases) ->
  literature; several sources -> calculated (an aggregate); none -> ''.
  `reference` keeps Frida's SourceID(s); look them up at
  https://fcdb.fooddata.dk/source/<release>/<SourceID>.
* License: dataset CC BY 4.0 (DTU Data, https://doi.org/10.11583/DTU.32312844).
  Attribute "The Danish Food Composition Database (https://fcdb.fooddata.dk),
  version <v>, <month year>, National Food Institute, Technical University
  of Denmark" - the exact text is taken from the workbook's Readme sheet.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("error: openpyxl missing - run: pip install openpyxl")

FOOD_ID_BASE = 60_000_000          # + FoodID; clear of FDC/BLS/INDB/TBCA
NUTRIENT_ID_BASE = 70_000_000      # food_nutrient ids
CATEGORY_ID_BASE = 60_000          # + FoodGroupID
DEFAULT_SOURCE_DIR = Path("source") / "frida"   # what download_frida.py fills
DEFAULT_CITATION = ("The Danish Food Composition Database "
                    "(https://fcdb.fooddata.dk), National Food Institute, "
                    "Technical University of Denmark")
DEFAULT_DOI = "https://doi.org/10.11583/DTU.32312844"

KJ_PER_KCAL = 4.184

# ---------------------------------------------------------------------------
# Canonical nutrient id -> [(Frida ParameterID, unit_factor, expected unit)]
# in priority order (first present per food wins). The expected unit is
# checked against the Parameter sheet so a unit change in a future release
# fails loudly instead of silently scaling values.
# ---------------------------------------------------------------------------
FRIDA_MAPPING = [
    (1,  [("356", 1, "kcal/100 g"),                    # Energy (kcal)
          ("137", round(1 / KJ_PER_KCAL, 6), "kJ/100g")]),  # kJ -> kcal
    (2,  [("172", 1, "g/100g")]),      # Available carbohydrates
    (3,  [("141", 1, "g/100g")]),      # Fat
    (4,  [("218", 1, "g/100g")]),      # Protein (N x Jones factor)
    (5,  [("245", 1, "g/100g")]),      # Sum sugars
    (6,  [("248", 1, "g/100g")]),      # Sum saturated fatty acids
    (7,  [("168", 1, "g/100g")]),      # Dietary fibre
    (8,  [("247", 1, "g/100g")]),      # Sum monounsaturated fatty acids
    (9,  [("251", 1, "g/100g")]),      # Sum polyunsaturated fatty acids
    (10, [("261", 1, "g/100g")]),      # Sum trans fatty acids
    (11, [("115", 1, "mg/100g")]),     # Cholesterol
    (12, [("201", 1, "mg/100g")]),     # Sodium
    (13, [("165", 1, "mg/100g")]),     # Potassium
    (14, [("184", 1, "mg/100g")]),     # Magnesium
    (15, [("108", 1, "mg/100g")]),     # Calcium
    (16, [("162", 1, "mg/100g")]),     # Iron
    (17, [("274", 1, "mg/100g")]),     # Zinc
    (18, [("214", 1, "mg/100g")]),     # Phosphorus
    # 19 vitamin A (ug RAE): derived, see VITA_* below
    (20, [("47",  1, "mg/100g")]),     # Vitamin C
    (21, [("126", 1, "µg/100g")]),     # Vitamin D (D2+D3+2.5x25-OH-D)
    (22, [("40",  1, "mg/100g")]),     # Vitamin B6
    (23, [("38",  1, "µg/100g")]),     # Vitamin B12
    (24, [("294", 1, "mg/100g")]),     # Niacin (preformed)
]

VITA_ID = 19
VITA_RE, RETINOL, BETA_CAROTENE = "12", "225", "303"
VITA_UNITS = {VITA_RE: "RE (µg/100g)", RETINOL: "µg/100g",
              BETA_CAROTENE: "µg/100g"}
# documented formulas -> nutrient_mapping rows (unit_factor 1, result ug RAE)
VITA_RULES = ["225+303/12", "(12+225)/2", "12-303/12"]


def vitamin_a_rae(values: dict) -> float | None:
    """ug RAE from Frida's RE / retinol / beta-carotene (any two suffice).
    Frida defines RE = retinol + beta-carotene/6; RAE uses /12."""
    re_, ret, bc = (values.get(VITA_RE), values.get(RETINOL),
                    values.get(BETA_CAROTENE))
    if ret is not None and bc is not None:
        rae = ret + bc / 12
    elif re_ is not None and ret is not None:
        rae = (re_ + ret) / 2
    elif re_ is not None and bc is not None:
        rae = re_ - bc / 12
    else:
        return None
    return max(rae, 0.0)


def short_title(description: str) -> str:
    """Concise display name: text before the first comma, capped at 80."""
    head = description.split(",")[0].strip()
    return (head or description.strip())[:80]


def num(value) -> float | None:
    """Workbook cells hold numbers, the string 'NULL', or nothing."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", ".")
    if not s or s.upper() == "NULL":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def text(value) -> str:
    s = "" if value is None else str(value).strip()
    return "" if s.upper() == "NULL" else s


def sheet_rows(wb, name: str):
    if name not in wb.sheetnames:
        sys.exit(f"error: sheet '{name}' missing from the workbook - is this "
                 f"an FCDB *_Dataset.xlsx? (found {wb.sheetnames})")
    return wb[name].iter_rows(values_only=True)


def header_index(header, required: list[str], sheet: str) -> dict[str, int]:
    col = {text(h): i for i, h in enumerate(header) if text(h)}
    for name in required:
        if name not in col:
            sys.exit(f"error: column '{name}' not found in sheet '{sheet}'")
    return col


def read_citation(wb) -> tuple[str, str]:
    """(long English citation, DOI URL) from the Readme sheet."""
    citation, doi = DEFAULT_CITATION, DEFAULT_DOI
    if "Readme" not in wb.sheetnames:
        return citation, doi
    for row in wb["Readme"].iter_rows(values_only=True):
        for cell in row:
            s = text(cell)
            if s.startswith("The Danish Food Composition Database (") \
                    and "version" in s:
                citation = re.sub(r"\s*\n\s*", ", ", s)
            m = re.search(r"https://doi\.org/10\.11583/DTU\.\d+", s)
            if m:
                doi = m.group(0)
    return citation, doi


def origin_for(source_field, source_type: dict) -> str:
    ids = [s.strip() for s in text(source_field).split(",") if s.strip()]
    if not ids:
        return ""
    if len(ids) > 1:
        return "calculated"                 # aggregate of several sources
    kind = source_type.get(ids[0], "")
    if kind == "E":
        return "calculated"                 # calculated / estimated / recipe / zero
    if kind == "L":
        return "label"
    return "literature"


def convert(data_xlsx: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.load_workbook(data_xlsx, read_only=True)
    citation, doi = read_citation(wb)

    # ---- Parameter sheet: verify the units of every mapped parameter ------
    rows = sheet_rows(wb, "Parameter")
    pcol = header_index(next(rows), ["ParameterID", "Unit"], "Parameter")
    units = {text(r[pcol["ParameterID"]]): text(r[pcol["Unit"]]) for r in rows}
    expected = {c: u for _, opts in FRIDA_MAPPING for c, _, u in opts}
    expected.update(VITA_UNITS)
    bad = [(c, u, units.get(c)) for c, u in expected.items() if units.get(c) != u]
    if bad:
        sys.exit("error: Frida parameter units changed - review FRIDA_MAPPING "
                 "factors before converting:\n" + "\n".join(
                     f"  ParameterID {c}: expected {u!r}, workbook has {g!r}"
                     for c, u, g in bad))

    # ---- Source sheet: SourceID -> EuroFIR reference type ------------------
    rows = sheet_rows(wb, "Source")
    scol = header_index(next(rows), ["SourceID", "EurofirRefType"], "Source")
    source_type = {text(r[scol["SourceID"]]): text(r[scol["EurofirRefType"]])
                   for r in rows}

    # ---- FoodGroup sheet: leaf groups -> food_category ---------------------
    rows = sheet_rows(wb, "FoodGroup")
    gcol = header_index(next(rows), ["FoodGroupID", "GroupNameUK"], "FoodGroup")
    group_name = {}
    for r in rows:
        gid = num(r[gcol["FoodGroupID"]])
        if gid is not None:
            group_name[int(gid)] = text(r[gcol["GroupNameUK"]])

    # ---- Food sheet: names + group -----------------------------------------
    rows = sheet_rows(wb, "Food")
    fcol = header_index(next(rows),
                        ["FoodID", "FoodName", "FødevareNavn", "FoodGroupID"],
                        "Food")
    foods, translations = [], []
    used_groups: set[int] = set()
    food_id_of: dict[int, int] = {}
    for r in rows:
        fid = num(r[fcol["FoodID"]])
        if fid is None or int(fid) in food_id_of:
            continue
        fid = int(fid)
        name_en = text(r[fcol["FoodName"]])
        name_da = text(r[fcol["FødevareNavn"]])
        if not (name_en or name_da):
            continue
        gid = num(r[fcol["FoodGroupID"]])
        cat = ""
        if gid is not None and int(gid) in group_name:
            used_groups.add(int(gid))
            cat = CATEGORY_ID_BASE + int(gid)
        food_id = FOOD_ID_BASE + fid
        food_id_of[fid] = food_id
        foods.append({
            "id": food_id,
            "source": "frida",
            "source_code": str(fid),
            "description": name_en or name_da,     # English; DA fallback
            "short_title": short_title(name_en or name_da),
            "food_category_id": cat,
            "publication_date": "",
        })
        if name_da:
            translations.append({"food_id": food_id, "locale": "da",
                                 "description": name_da, "source": "native"})

    # ---- Data_Normalised: one row per food x parameter ---------------------
    rows = sheet_rows(wb, "Data_Normalised")
    dcol = header_index(next(rows),
                        ["FoodID", "ParameterID", "ResVal", "Min", "Max",
                         "NumberOfDeterminations", "Source"],
                        "Data_Normalised")
    wanted = set(expected)
    values: dict[int, dict[str, float]] = {}
    meta: dict[tuple[int, str], tuple] = {}
    for r in rows:
        fid = num(r[dcol["FoodID"]])
        pid = text(r[dcol["ParameterID"]])
        if fid is None or pid not in wanted:
            continue
        amount = num(r[dcol["ResVal"]])
        if amount is None:
            continue
        fid = int(fid)
        values.setdefault(fid, {})[pid] = amount
        meta[(fid, pid)] = (num(r[dcol["Min"]]), num(r[dcol["Max"]]),
                            num(r[dcol["NumberOfDeterminations"]]),
                            text(r[dcol["Source"]]))
    wb.close()

    food_nutrients = []
    next_fn_id = NUTRIENT_ID_BASE

    def fmt(x: float | None, factor: float = 1) -> str | float:
        return "" if x is None else round(x * factor, 6)

    for fid, food_id in food_id_of.items():
        vals = values.get(fid, {})
        for nid, options in FRIDA_MAPPING:
            for code, factor, _unit in options:
                if code not in vals:
                    continue
                lo, hi, n, src = meta[(fid, code)]
                food_nutrients.append({
                    "id": next_fn_id, "food_id": food_id, "nutrient_id": nid,
                    "amount": fmt(vals[code], factor),
                    "origin": origin_for(src, source_type),
                    "reference": src,
                    "data_points": "" if n is None else int(n),
                    "min": fmt(lo, factor), "max": fmt(hi, factor),
                })
                next_fn_id += 1
                break                         # first available option wins
        rae = vitamin_a_rae(vals)
        if rae is not None:
            food_nutrients.append({
                "id": next_fn_id, "food_id": food_id, "nutrient_id": VITA_ID,
                "amount": round(rae, 6), "origin": "calculated",
                "reference": "", "data_points": "", "min": "", "max": "",
            })
            next_fn_id += 1

    # ------------------------------------------------------------------ write
    def write(name: str, rows_, columns: list[str]) -> None:
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_)
        print(f"  {name:20s} {len(rows_):>7,} rows")

    print("Writing Supabase CSVs:")
    write("food_source",
          [{"code": "frida",
            "name": "Danish Food Composition Database (Frida), "
                    "DTU National Food Institute",
            "license_note": f"CC BY 4.0 - {citation}. {doi}"}],
          ["code", "name", "license_note"])
    write("nutrient_mapping",
          [{"source": "frida", "source_component_code": c,
            "nutrient_id": nid, "unit_factor": f}
           for nid, opts in FRIDA_MAPPING for c, f, _u in opts] +
          [{"source": "frida", "source_component_code": rule,
            "nutrient_id": VITA_ID, "unit_factor": 1} for rule in VITA_RULES],
          ["source", "source_component_code", "nutrient_id", "unit_factor"])
    write("food_category",
          [{"id": CATEGORY_ID_BASE + g, "source": "frida",
            "description": group_name[g]} for g in sorted(used_groups)],
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
    print(f"\nDone. {len(foods):,} Frida foods ({n_energy:,} with energy, "
          f"{len(used_groups)} categories) -> {out_dir}")
    print(f"Attribution: {citation}. {doi} (CC BY 4.0)")


def discover(source_dir: Path) -> Path:
    """Newest FCDB *Dataset*.xlsx under source_dir (download_frida.py)."""
    hits = [p for p in source_dir.rglob("*.xlsx")
            if "dataset" in p.name.lower() and not p.name.startswith("~$")]
    if not hits:
        sys.exit(f"error: no *Dataset*.xlsx under {source_dir} - run "
                 f"download_frida.py first, or pass --data")

    def version(p: Path) -> tuple:
        return tuple(int(x) for x in re.findall(r"\d+", p.name)) or (0,)
    hits.sort(key=version)
    if len(hits) > 1:
        print(f"  note: {len(hits)} workbooks found, using {hits[-1].name}",
              file=sys.stderr)
    return hits[-1]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert the Danish FCDB (Frida) xlsx to multi-source "
                    "Supabase CSVs")
    ap.add_argument("--data", type=Path, default=None,
                    help="FCDB_*_Dataset.xlsx (default: found under --source-dir)")
    ap.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR,
                    help="folder from download_frida.py (default: ./source/frida)")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    data = args.data or discover(args.source_dir)
    if not data.exists():
        sys.exit(f"error: {data} not found")
    print(f"data: {data}")
    convert(data, args.output)


if __name__ == "__main__":
    main()
