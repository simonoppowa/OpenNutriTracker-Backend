#!/usr/bin/env python3
"""Convert USDA Food Data Central downloads (Foundation, SR Legacy,
FNDDS/Survey and/or Branded) into CSV files matching the multi-source
Supabase schema (multisource_food_schema.mermaid).

Branded foods (data_type 'branded_food') add brand + barcode + serving
from branded_food.csv: gtin_upc and brand fill market_acquisition
(-> MealDBO.code / MealDBO.brands), serving_size fills food_portion.
Branded is huge (~2M foods, ~28M nutrient rows), so its food.csv and
food_nutrient.csv are streamed row-by-row rather than read into memory.

Emitted CSVs (one per table, FK-safe load order):

    1. food_source        6. food             10. market_acquisition
    2. nutrient           7. food_nutrient    11. input_food
    3. nutrient_mapping   8. measure_unit     12. food_component
    4. food_category      9. food_portion     13. retention_factor
                                              14. food_image

Not emitted: food_summary (materialized view), food_translation (FDC is
English-only; food.description already holds it), bls_component and the
*_translation tables (no FDC data for them).

Usage:
    # --input accepts extracted folders AND official FDC .zip downloads
    # (Foundation, SR Legacy, FNDDS/Survey), in any mix:
    python fdc_to_ont_csv.py \
        --input FoodData_Central_foundation_food_csv_2025-04-24.zip \
        --input FoodData_Central_sr_legacy_food_csv_2018-04.zip \
        --input FoodData_Central_survey_food_csv_2024-10-31.zip \
        --output <out_dir> [--images <manifest.csv>]

    # or convert every FoodData_Central*.zip in a folder:
    python fdc_to_ont_csv.py --zip-dir ~/Downloads --output <out_dir>

    # with no input flags it reads ./source (what download_fdc.py fills,
    # holding foundation/ sr_legacy/ fndds/ subdirs):
    python fdc_to_ont_csv.py --output <out_dir>

Typical pipeline:
    python download_fdc.py --output source        # download + extract
    python fdc_to_ont_csv.py --output ../../out/fdc  # reads ./source

Get the ZIPs from https://fdc.nal.usda.gov/download-datasets
("Full Download of All Data Types" per dataset, CSV format).
Zips are extracted to a temp dir and cleaned up automatically.

Images
------
FDC ships no food photos, so food_image.csv is empty unless you pass
--images with a manifest CSV of your own:

    fdc_id,kind,storage_path,external_url,width,height,attribution,ai_generated
    321358,thumbnail,food-images/fdc/321358_thumb.webp,,320,320,,false
    321358,main,,https://example.com/hummus.jpg,1200,900,Jane Doe,false
    167512,main,food-images/gen/167512.webp,,1024,1024,,true

kind must be 'thumbnail' or 'main'; exactly one of storage_path /
external_url must be set; ai_generated defaults to false when empty. storage_path is the object key inside your
Supabase Storage bucket (e.g. 'food-images'). Rows whose fdc_id is not a
converted food are skipped with a warning.

Notes
-----
* food.id reuses the numeric fdc_id as surrogate key. Give other sources
  (BLS...) ids above 10_000_000 to avoid collisions.
* Only foundation_food / sr_legacy_food rows become foods. Lab provenance
  rows (sample_food, market_acquisition, sub_sample_food...) are skipped;
  as a consequence market_acquisition.csv (UPC codes live on sample foods)
  is usually empty for these two datasets - it is emitted for schema
  completeness and future branded imports.
* input_food.input_food_id is blanked when it points at a skipped sample
  food (the FK could not resolve); the row is kept for the gram weights.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical nutrients (= MealNutrimentsDBO fields in the app).
# FDC ids listed in priority order; first one present per food wins.
# All preferred ids already report in the canonical unit -> unit_factor 1.
# ---------------------------------------------------------------------------
CANONICAL_NUTRIENTS = [
    # (id, name,                       unit,   rank, FDC nutrient ids)
    (1,  "Energy",                     "KCAL", 10,  ["1008", "2047", "2048"]),
    (2,  "Carbohydrates",              "G",    20,  ["1005"]),
    (3,  "Fat",                        "G",    30,  ["1004"]),
    (4,  "Protein",                    "G",    40,  ["1003"]),
    (5,  "Sugars",                     "G",    50,  ["2000"]),
    (6,  "Saturated fat",              "G",    60,  ["1258"]),
    (7,  "Fiber",                      "G",    70,  ["1079"]),
    (8,  "Monounsaturated fat",        "G",    80,  ["1292"]),
    (9,  "Polyunsaturated fat",        "G",    90,  ["1293"]),
    (10, "Trans fat",                  "G",    100, ["1257"]),
    (11, "Cholesterol",                "MG",   110, ["1253"]),
    (12, "Sodium",                     "MG",   120, ["1093"]),
    (13, "Potassium",                  "MG",   130, ["1092"]),
    (14, "Magnesium",                  "MG",   140, ["1090"]),
    (15, "Calcium",                    "MG",   150, ["1087"]),
    (16, "Iron",                       "MG",   160, ["1089"]),
    (17, "Zinc",                       "MG",   170, ["1095"]),
    (18, "Phosphorus",                 "MG",   180, ["1091"]),
    (19, "Vitamin A (RAE)",            "UG",   190, ["1106"]),
    (20, "Vitamin C",                  "MG",   200, ["1162"]),
    (21, "Vitamin D",                  "UG",   210, ["1114"]),
    (22, "Vitamin B6",                 "MG",   220, ["1175"]),
    (23, "Vitamin B12",                "UG",   230, ["1178"]),
    (24, "Niacin (B3)",                "MG",   240, ["1167"]),
]

SOURCE_BY_DATA_TYPE = {
    "foundation_food": "fdc_foundation",
    "sr_legacy_food": "fdc_sr_legacy",
    "survey_fndds_food": "fdc_survey",
    "branded_food": "fdc_branded",
}

FOOD_SOURCES = [
    ("fdc_foundation", "USDA FoodData Central - Foundation Foods",
     "Public domain (CC0), attribution appreciated"),
    ("fdc_sr_legacy", "USDA FoodData Central - SR Legacy",
     "Public domain (CC0), attribution appreciated"),
    ("fdc_survey", "USDA FoodData Central - FNDDS Survey Foods",
     "Public domain (CC0), attribution appreciated"),
    ("fdc_branded", "USDA FoodData Central - Branded Foods",
     "Public domain (CC0); label data (c) the respective manufacturers"),
]

UNDETERMINED_UNIT_ID = "9999"        # FDC measure_unit 'undetermined'
BRANDED_PORTION_ID_BASE = 900_000_000  # synthetic ids, clear of real ones
DEFAULT_SOURCE_DIR = Path("source")  # what download_fdc.py fills by default

# origin vocabulary: first letter of FDC derivation code.
ORIGIN_BY_PREFIX = {"A": "analysis", "M": "label", "L": "label"}
DEFAULT_ORIGIN = "calculated"


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def stream_csv(path: Path):
    """Yield rows one at a time - for the big Branded files (food.csv ~2M
    rows, food_nutrient.csv ~28M) that must not be loaded into memory."""
    if not path.exists():
        return
    with open(path, newline="", encoding="utf-8-sig") as fh:
        yield from csv.DictReader(fh)


def maybe(path: Path) -> list[dict]:
    return read_csv(path) if path.exists() else []


def cell(row: dict, key: str) -> str:
    v = row.get(key) or ""
    return v.strip()


def short_title(description: str) -> str:
    """Concise display name: the text before the first comma, capped at
    80 chars (FDC descriptions read 'Milk, whole, 3.25% milkfat, ...')."""
    head = description.split(",")[0].strip()
    return (head or description.strip())[:80]


def convert(input_dirs: list[Path], out_dir: Path,
            images_manifest: Path | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    nutrient_by_fdc = {}      # fdc nutrient id -> (canonical id, priority)
    for nid, _, _, _, fdc_ids in CANONICAL_NUTRIENTS:
        for prio, fid in enumerate(fdc_ids):
            nutrient_by_fdc[fid] = (nid, prio)

    foods: dict[str, dict] = {}
    categories: dict[str, dict] = {}
    measure_units: dict[str, dict] = {}
    portions: list[dict] = []
    market_acq: list[dict] = []
    input_foods: list[dict] = []
    components: list[dict] = []
    retention: dict[str, dict] = {}
    best: dict[tuple, tuple] = {}   # (food_id, nutrient_id) -> (prio, row)

    for in_dir in input_dirs:
        derivations = {r["id"]: r for r in maybe(in_dir / "food_nutrient_derivation.csv")}

        # FNDDS (survey) quirk: its food_nutrient.csv references nutrients by
        # legacy nutrient NUMBER (301=calcium) instead of FDC nutrient id
        # (1087). Build a number->id resolver from this dir's nutrient.csv.
        nbr_to_id = {}
        for r in maybe(in_dir / "nutrient.csv"):
            nbr = cell(r, "nutrient_nbr")
            if nbr:
                nbr_to_id[nbr.removesuffix(".0")] = r["id"]

        # ---- food -------------------------------------------------------
        included: set[str] = set()
        for r in stream_csv(in_dir / "food.csv"):
            source = SOURCE_BY_DATA_TYPE.get(r["data_type"])
            if not source:
                continue                       # skip lab-provenance rows
            fdc_id = r["fdc_id"]
            included.add(fdc_id)
            foods[fdc_id] = {
                "id": fdc_id,
                "source": source,
                "source_code": fdc_id,
                "description": r["description"],
                "short_title": short_title(r["description"]),
                "food_category_id": cell(r, "food_category_id"),
                "publication_date": cell(r, "publication_date"),
            }

        # ---- food_category ------------------------------------------------
        for r in maybe(in_dir / "food_category.csv"):
            categories.setdefault(r["id"], {
                "id": r["id"], "source": "", "description": r["description"],
            })
        # WWEIA categories (FNDDS): ids 1002-9999, no overlap with 1-28.
        for r in maybe(in_dir / "wweia_food_category.csv"):
            categories.setdefault(r["wweia_food_category"], {
                "id": r["wweia_food_category"],
                "source": "fdc_survey",
                "description": r["wweia_food_category_description"],
            })

        # ---- measure_unit / food_portion ----------------------------------
        for r in maybe(in_dir / "measure_unit.csv"):
            measure_units.setdefault(r["id"], {"id": r["id"], "name": r["name"]})
        for r in maybe(in_dir / "food_portion.csv"):
            if r["fdc_id"] in included and cell(r, "gram_weight"):
                portions.append({
                    "id": r["id"],
                    "food_id": r["fdc_id"],
                    "amount": cell(r, "amount"),
                    "measure_unit_id": r["measure_unit_id"],
                    "portion_description": cell(r, "portion_description"),
                    "modifier": cell(r, "modifier"),
                    "seq_num": cell(r, "seq_num"),
                    "gram_weight": r["gram_weight"],
                })

        # ---- branded_food -> market_acquisition (brand + UPC) + serving --
        # Branded foods keep brand/barcode/serving in branded_food.csv.
        # gtin_upc -> MealDBO.code (barcode); brand -> MealDBO.brands.
        for r in stream_csv(in_dir / "branded_food.csv"):
            fdc_id = r["fdc_id"]
            if fdc_id not in included:
                continue
            # branded ships a curated short_description - prefer it
            sd = cell(r, "short_description")
            if sd:
                foods[fdc_id]["short_title"] = sd[:80]
            brand = cell(r, "brand_name") or cell(r, "brand_owner")
            upc = cell(r, "gtin_upc")
            if brand or upc:
                market_acq.append({
                    "food_id": fdc_id,
                    "upc_code": upc,
                    "brand_description": brand,
                })
            # serving -> a single food_portion (gram_weight only when in grams)
            size = cell(r, "serving_size")
            unit = cell(r, "serving_size_unit").lower()
            household = cell(r, "household_serving_fulltext")
            if size and unit in ("g", "gm", "grm"):
                measure_units.setdefault(UNDETERMINED_UNIT_ID,
                                         {"id": UNDETERMINED_UNIT_ID,
                                          "name": "undetermined"})
                portions.append({
                    "id": BRANDED_PORTION_ID_BASE + len(portions),
                    "food_id": fdc_id,
                    "amount": "1",
                    "measure_unit_id": UNDETERMINED_UNIT_ID,
                    "portion_description": household or f"{size} {unit}",
                    "modifier": "",
                    "seq_num": "1",
                    "gram_weight": size,
                })

        # ---- food_nutrient -------------------------------------------------
        for r in stream_csv(in_dir / "food_nutrient.csv"):
            rid = r["nutrient_id"]
            if rid not in nutrient_by_fdc:            # FNDDS legacy number?
                rid = nbr_to_id.get(rid, rid)
            hit = nutrient_by_fdc.get(rid)
            if not hit or r["fdc_id"] not in included or not cell(r, "amount"):
                continue
            nid, prio = hit
            key = (r["fdc_id"], nid)
            if key in best and best[key][0] <= prio:
                continue
            deriv = derivations.get(cell(r, "derivation_id"))
            origin = (ORIGIN_BY_PREFIX.get(deriv["code"][:1].upper(), DEFAULT_ORIGIN)
                      if deriv else "")
            best[key] = (prio, {
                "id": r["id"],
                "food_id": r["fdc_id"],
                "nutrient_id": nid,
                "amount": r["amount"],
                "origin": origin,
                "reference": cell(r, "footnote"),
                "data_points": cell(r, "data_points"),
                "min": cell(r, "min"),
                "max": cell(r, "max"),
            })

        # ---- FUTURE side tables ----------------------------------------------
        for r in maybe(in_dir / "market_acquisition.csv"):
            if r["fdc_id"] in included:        # usually none (samples skipped)
                market_acq.append({
                    "food_id": r["fdc_id"],
                    "upc_code": cell(r, "upc_code"),
                    "brand_description": cell(r, "brand_description"),
                })
        for r in maybe(in_dir / "input_food.csv"):
            if r["fdc_id"] not in included:
                continue
            ref = cell(r, "fdc_of_input_food")
            input_foods.append({
                "id": r["id"],
                "food_id": r["fdc_id"],
                "input_food_id": ref if ref in included else "",
                "gram_weight": cell(r, "gram_weight"),
            })
        for r in maybe(in_dir / "food_component.csv"):
            if r["fdc_id"] in included:
                components.append({
                    "id": r["id"],
                    "food_id": r["fdc_id"],
                    "name": r["name"],
                    "pct_weight": cell(r, "pct_weight"),
                    "is_refuse": "true" if cell(r, "is_refuse").upper() == "Y" else "false",
                })
        for r in maybe(in_dir / "retention_factor.csv"):
            retention.setdefault(r["id"], {
                "id": r["id"], "code": r["code"], "description": r["description"],
            })

    # ---- food_image (from optional user-provided manifest) -----------------
    images: list[dict] = []
    if images_manifest:
        for i, r in enumerate(read_csv(images_manifest), start=1):
            fdc_id = cell(r, "fdc_id")
            kind = cell(r, "kind")
            path, url = cell(r, "storage_path"), cell(r, "external_url")
            if fdc_id not in foods:
                print(f"  warning: image row {i} skipped, "
                      f"unknown fdc_id {fdc_id}", file=sys.stderr)
                continue
            if kind not in ("thumbnail", "main") or bool(path) == bool(url):
                print(f"  warning: image row {i} skipped, bad kind or "
                      f"storage_path/external_url", file=sys.stderr)
                continue
            ai = cell(r, "ai_generated").lower()
            if ai not in ("", "true", "false"):
                print(f"  warning: image row {i} skipped, ai_generated "
                      f"must be true/false", file=sys.stderr)
                continue
            images.append({
                "id": len(images) + 1,
                "food_id": fdc_id,
                "kind": kind,
                "storage_path": path,
                "external_url": url,
                "width": cell(r, "width"),
                "height": cell(r, "height"),
                "attribution": cell(r, "attribution"),
                "ai_generated": ai or "false",
            })

    # ------------------------------------------------------------------ write
    def write(name: str, rows, columns: list[str]) -> None:
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"  {name:22s} {len(rows):>7,} rows")

    print("Writing Supabase CSVs:")
    write("food_source",
          [{"code": c, "name": n, "license_note": l} for c, n, l in FOOD_SOURCES],
          ["code", "name", "license_note"])
    write("nutrient",
          [{"id": i, "name": n, "unit_name": u, "rank": rk}
           for i, n, u, rk, _ in CANONICAL_NUTRIENTS],
          ["id", "name", "unit_name", "rank"])
    write("nutrient_mapping",
          [{"source": "fdc", "source_component_code": fid,
            "nutrient_id": i, "unit_factor": 1}
           for i, _, _, _, fids in CANONICAL_NUTRIENTS for fid in fids],
          ["source", "source_component_code", "nutrient_id", "unit_factor"])
    write("food_category", categories.values(), ["id", "source", "description"])
    write("food", foods.values(),
          ["id", "source", "source_code", "description", "short_title",
           "food_category_id", "publication_date"])
    write("food_nutrient", [row for _, row in best.values()],
          ["id", "food_id", "nutrient_id", "amount", "origin",
           "reference", "data_points", "min", "max"])
    write("measure_unit", measure_units.values(), ["id", "name"])
    write("food_portion", portions,
          ["id", "food_id", "amount", "measure_unit_id",
           "portion_description", "modifier", "seq_num", "gram_weight"])
    write("market_acquisition", market_acq,
          ["food_id", "upc_code", "brand_description"])
    write("input_food", input_foods,
          ["id", "food_id", "input_food_id", "gram_weight"])
    write("food_component", components,
          ["id", "food_id", "name", "pct_weight", "is_refuse"])
    write("retention_factor", retention.values(), ["id", "code", "description"])
    write("food_image", images,
          ["id", "food_id", "kind", "storage_path", "external_url",
           "width", "height", "attribution", "ai_generated"])

    n_energy = len({k[0] for k in best if k[1] == 1})
    print(f"\nDone. {len(foods):,} foods ({n_energy:,} with energy) -> {out_dir}")


def extract_and_locate(zip_path: Path, tmp_root: Path) -> Path:
    """Unzip an FDC download and return the dir containing food.csv."""
    dest = tmp_root / zip_path.stem
    print(f"extracting {zip_path.name} ...")
    with zipfile.ZipFile(zip_path) as zf:
        bad = [n for n in zf.namelist()
               if n.startswith(("/", "..")) or ".." in Path(n).parts]
        if bad:
            sys.exit(f"error: {zip_path.name} contains unsafe paths: {bad[:3]}")
        zf.extractall(dest)
    hits = sorted(dest.rglob("food.csv"), key=lambda p: len(p.parts))
    if not hits:
        sys.exit(f"error: no food.csv inside {zip_path.name} - "
                 f"is this a Food Data Central CSV download?")
    return hits[0].parent


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert FDC CSV downloads (dirs or zips) to "
                    "multi-source Supabase CSVs")
    ap.add_argument("--input", action="append", default=[], type=Path,
                    help="FDC download dir or .zip (repeat per dataset)")
    ap.add_argument("--zip-dir", type=Path, default=None,
                    help="also convert every FoodData_Central*.zip here")
    ap.add_argument("--source-dir", type=Path, default=None,
                    help="folder from download_fdc.py; auto-discovers any "
                         "subdir containing food.csv (e.g. foundation/, "
                         "sr_legacy/, fndds/). Defaults to ./source when no "
                         "other input is given.")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--images", type=Path, default=None,
                    help="optional food_image manifest CSV (see docstring)")
    args = ap.parse_args()

    inputs = list(args.input)
    if args.zip_dir:
        inputs += sorted(args.zip_dir.glob("FoodData_Central*.zip"))

    # Resolve the source dir. If nothing was passed at all, fall back to
    # ./source (what download_fdc.py fills) - the default workflow.
    source_dir = args.source_dir
    if source_dir is None and not inputs:
        source_dir = DEFAULT_SOURCE_DIR
        print(f"no --input/--zip-dir/--source-dir given; "
              f"using ./{DEFAULT_SOURCE_DIR}")
    if source_dir is not None:
        if not source_dir.is_dir():
            sys.exit(f"error: source dir {source_dir} not found - "
                     f"run download_fdc.py first (or pass --input/--zip-dir)")
        found = sorted(p.parent for p in source_dir.glob("*/food.csv"))
        if not found:
            sys.exit(f"error: no <subdir>/food.csv under {source_dir} - "
                     f"did download_fdc.py finish extracting?")
        print(f"source-dir: found {len(found)} dataset(s): "
              f"{', '.join(p.name for p in found)}")
        inputs += found
    if not inputs:
        ap.error("no inputs: use --input, --zip-dir, and/or --source-dir")
    if args.images and not args.images.exists():
        sys.exit(f"error: {args.images} not found")

    with tempfile.TemporaryDirectory(prefix="fdc_extract_") as tmp:
        input_dirs = []
        for p in inputs:
            if not p.exists():
                sys.exit(f"error: {p} not found")
            if p.is_file() and zipfile.is_zipfile(p):
                input_dirs.append(extract_and_locate(p, Path(tmp)))
            elif p.is_dir() and (p / "food.csv").exists():
                input_dirs.append(p)
            else:
                sys.exit(f"error: {p} is neither an FDC zip nor a dir "
                         f"containing food.csv")
        convert(input_dirs, args.output, args.images)


if __name__ == "__main__":
    main()
