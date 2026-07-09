#!/usr/bin/env python3
"""Validate imported food data against the ORIGINAL source files.

Samples random foods from the database (or from converted CSVs) and, for
each, re-derives the expected values straight from the raw FDC / BLS
downloads - independently of the converters - then compares. Catches
import bugs, unit-conversion drift, and truncation that a converter
self-test would miss.

Per sampled food it checks:
    * description        == raw source name
    * short_title        == first comma-segment of the raw name (BLS/FDC)
    * all 24 canonical nutrients (energy, macros, minerals, vitamins)
      in food_summary == the raw values, resolved with the same
      per-nutrient priority + unit factors the converter uses (NULL-aware,
      within a 2% tolerance for import rounding)
    * BLS only: German food_translation == raw 'Lebensmittelbezeichnung'

By default it checks the Supabase tables (food + food_summary +
food_translation) against the raw source data. Pass --csv-dir to instead
validate converted CSVs offline before importing.

Usage:
    # check Supabase against ./source (uses $SUPABASE_DB_URL):
    python test_against_source.py --source-dir source --samples 50

    # or an explicit connection string:
    python test_against_source.py --db "postgresql://..." --source-dir source

    # offline: validate converted CSVs (no DB):
    python test_against_source.py --csv-dir out/fdc --source-dir source \
        --source fdc --samples 100

Requires: psycopg2-binary (for --db), openpyxl (for BLS).
Exit code 0 = all checks passed, 1 = at least one mismatch.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fdc"))
sys.path.insert(0, str(HERE / "bls"))
sys.path.insert(0, str(HERE / "shared"))

from fdc_to_ont_csv import (CANONICAL_NUTRIENTS, SOURCE_BY_DATA_TYPE,  # noqa: E402
                            short_title as fdc_short)
from bls_to_ont_csv import (BLS_MAPPING, parse_amount,                 # noqa: E402
                            short_title as bls_short)

TOL = 0.02          # 2% relative tolerance for floats (rounding at import)

# canonical nutrient id -> food_summary column name (fixed by schema.sql)
NUTRIENT_COLUMN = {
    1: "energy_kcal_100",       2: "carbohydrates_100",  3: "fat_100",
    4: "proteins_100",          5: "sugars_100",         6: "saturated_fat_100",
    7: "fiber_100",             8: "monounsaturated_fat_100",
    9: "polyunsaturated_fat_100", 10: "trans_fat_100",  11: "cholesterol_100",
    12: "sodium_100",           13: "potassium_100",    14: "magnesium_100",
    15: "calcium_100",          16: "iron_100",         17: "zinc_100",
    18: "phosphorus_100",       19: "vitamin_a_100",    20: "vitamin_c_100",
    21: "vitamin_d_100",        22: "vitamin_b6_100",   23: "vitamin_b12_100",
    24: "niacin_100",
}
# reverse: FDC nutrient id -> (canonical id, priority) - same as the converter
FDC_TO_CANON = {}
for _nid, *_rest, _fids in CANONICAL_NUTRIENTS:
    for _p, _fid in enumerate(_fids):
        FDC_TO_CANON[_fid] = (_nid, _p)


# ----------------------------------------------------------------- helpers
def approx(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    a, b = float(a), float(b)
    return abs(a - b) <= max(TOL * max(abs(a), abs(b)), 1e-6)


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


# ----------------------------------------------------------------- FDC origin
def build_fdc_index(source_dir: Path):
    """fdc_id -> (dataset_dir, description). Scans every */food.csv."""
    idx = {}
    for food_csv in sorted(source_dir.glob("*/food.csv")):
        for r in read_csv(food_csv):
            if r["data_type"] in SOURCE_BY_DATA_TYPE:
                idx[r["fdc_id"]] = (food_csv.parent, r["description"])
    return idx


def fdc_expected_nutrients(dataset_dir: Path, wanted: set[str]) -> dict:
    """{fdc_id: {canonical_id: amount}} for the wanted ids - all 24
    nutrients, using the app's per-nutrient priority and the FNDDS
    legacy nutrient-number fallback. One pass over food_nutrient.csv."""
    nbr_to_id = {}
    npath = dataset_dir / "nutrient.csv"
    if npath.exists():
        for r in read_csv(npath):
            nbr = (r.get("nutrient_nbr") or "").strip().removesuffix(".0")
            if nbr:
                nbr_to_id[nbr] = r["id"]
    # per food: {canonical_id: (priority, amount)}
    best: dict[str, dict] = {fid: {} for fid in wanted}
    with open(dataset_dir / "food_nutrient.csv", newline="",
              encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            fid = r["fdc_id"]
            if fid not in wanted or not (r.get("amount") or "").strip():
                continue
            rid = r["nutrient_id"]
            if rid not in FDC_TO_CANON:
                rid = nbr_to_id.get(rid, rid)     # FNDDS number -> id
            hit = FDC_TO_CANON.get(rid)
            if not hit:
                continue
            nid, prio = hit
            cur = best[fid].get(nid)
            if cur is None or prio < cur[0]:
                best[fid][nid] = (prio, float(r["amount"]))
    return {fid: {nid: v[1] for nid, v in d.items()} for fid, d in best.items()}


# ----------------------------------------------------------------- BLS origin
def build_bls_index(source_dir: Path):
    """BLS code -> {name_de, name_en, nutrients:{canonical_id: amount}}.
    Applies BLS_MAPPING (code + unit_factor, per-nutrient priority) to
    each row - the same conversion the converter performs."""
    try:
        import openpyxl
    except ImportError:
        sys.exit("error: openpyxl needed for BLS validation")
    hits = sorted((source_dir / "bls").rglob("*Daten*.xlsx")) \
        or sorted(source_dir.rglob("*Daten*.xlsx"))
    if not hits:
        return {}
    ws = openpyxl.load_workbook(hits[0], read_only=True).active
    rows = ws.iter_rows(values_only=True)
    header = [str(h) if h else "" for h in next(rows)]

    # BLS component code -> value column index (header 'CODE label [unit/100g]')
    code_col = {}
    for i, h in enumerate(header):
        if "/100g]" in h:
            code_col[h.split(" ", 1)[0]] = i

    idx = {}
    for row in rows:
        code = str(row[0] or "").strip()
        if not code:
            continue
        nutrients = {}
        for nid, options in BLS_MAPPING:
            for bls_code, factor in options:          # first present wins
                col = code_col.get(bls_code)
                if col is None:
                    continue
                amount = parse_amount(row[col])
                if amount is not None:
                    nutrients[nid] = round(amount * factor, 6)
                    break
        idx[code] = {
            "name_de": str(row[1] or "").strip(),
            "name_en": str(row[2] or "").strip(),
            "nutrients": nutrients,
        }
    return idx


# ----------------------------------------------------------------- actual rows
def sample_from_db(db: str, n: int, sources: list[str]):
    import psycopg2
    from import_fdc import normalize_db_url
    conn = psycopg2.connect(normalize_db_url(db))
    ncols = ", ".join(f"s.{NUTRIENT_COLUMN[i]}" for i in range(1, 25))
    with conn.cursor() as cur:
        cur.execute(f"""
            select f.source, f.source_code, f.description, f.short_title,
                   (select t.description from food_translation t
                    where t.food_id = f.id and t.locale = 'de') as de,
                   {ncols}
            from food f join food_summary s on s.food_id = f.id
            where split_part(f.source,'_',1) = any(%s)
            order by random() limit %s""", (list(sources), n))
        cols = [c[0] for c in cur.description]
        rows = []
        for raw in cur.fetchall():
            d = dict(zip(cols, raw))
            nutrients = {i: d.pop(NUTRIENT_COLUMN[i]) for i in range(1, 25)}
            d["nutrients"] = {k: (float(v) if v is not None else None)
                              for k, v in nutrients.items()}
            rows.append(d)
    conn.close()
    return rows


def sample_from_csv(csv_dir: Path, n: int):
    foods = read_csv(csv_dir / "food.csv")
    nut = {}                                   # food_id -> {canonical_id: amount}
    fn = csv_dir / "food_nutrient.csv"
    if fn.exists():
        for r in read_csv(fn):
            if (r.get("amount") or "").strip():
                nut.setdefault(r["food_id"], {})[int(r["nutrient_id"])] = \
                    float(r["amount"])
    de = {}
    ft = csv_dir / "food_translation.csv"
    if ft.exists():
        for r in read_csv(ft):
            if r.get("locale") == "de":
                de[r["food_id"]] = r["description"]
    picks = random.sample(foods, min(n, len(foods)))
    out = []
    for r in picks:
        vals = nut.get(r["id"], {})
        out.append({
            "source": r["source"], "source_code": r["source_code"],
            "description": r["description"], "short_title": r["short_title"],
            "de": de.get(r["id"]),
            "nutrients": {i: vals.get(i) for i in range(1, 25)},
        })
    return out


# ----------------------------------------------------------------- validation
def preflight(rows, source_dir: Path) -> None:
    """Fail loudly if the raw source-dir clearly lacks the data for the
    sampled sources - avoids 50 confusing 'in-source' failures."""
    fams = {r["source"].split("_")[0] for r in rows}
    if "fdc" in fams and not any(source_dir.glob("*/food.csv")):
        sys.exit(f"error: no FDC dataset (*/food.csv) under {source_dir}/ - "
                 f"wrong --source-dir? the download folder holds "
                 f"foundation/ sr_legacy/ fndds/ subdirs.")
    if "bls" in fams and not (list((source_dir / "bls").rglob("*Daten*.xlsx"))
                              or list(source_dir.rglob("*Daten*.xlsx"))):
        sys.exit(f"error: no BLS *Daten*.xlsx under {source_dir}/bls - "
                 f"wrong --source-dir? run download_bls.py first.")


def validate(rows, source_dir: Path, verbose: bool):
    preflight(rows, source_dir)
    fdc_idx = None
    bls_idx = None
    checks = 0
    fails = 0
    per_source = {}          # source -> [checks, fails]
    per_field = {}           # field  -> [checks, fails]

    def check(cond, source, code, field, actual, expected):
        nonlocal checks, fails
        checks += 1
        ps = per_source.setdefault(source, [0, 0])
        pf = per_field.setdefault(field, [0, 0])
        ps[0] += 1
        pf[0] += 1
        ok = bool(cond)
        if not ok:
            fails += 1
            ps[1] += 1
            pf[1] += 1
            print(f"  [FAIL] {source:14} {code:10} {field:20} "
                  f"db={actual!r}  expected={expected!r}")
        elif verbose:
            print(f"  [ ok ] {source:14} {code:10} {field:20} = {actual!r}")

    def check_nutrients(source, code, db_nut, exp_nut):
        """Compare all 24 canonical nutrients (NULL-aware)."""
        for nid in range(1, 25):
            check(approx(db_nut.get(nid), exp_nut.get(nid)),
                  source, code, NUTRIENT_COLUMN[nid],
                  db_nut.get(nid), exp_nut.get(nid))

    for i, r in enumerate(rows, 1):
        fam = r["source"].split("_")[0]
        code = r["source_code"]
        if verbose:
            print(f"\n[{i}/{len(rows)}] {r['source']} {code} "
                  f"— {str(r.get('description'))[:50]!r}")
        if fam == "fdc":
            if fdc_idx is None:
                if verbose:
                    print("  (loading FDC raw index ...)")
                fdc_idx = build_fdc_index(source_dir)
            if code not in fdc_idx:
                check(False, r["source"], code, "in-source", "missing", "present")
                continue
            dataset, raw_desc = fdc_idx[code]
            if verbose:
                print(f"  raw dataset: {dataset.name}")
            check(r["description"] == raw_desc, r["source"], code,
                  "description", r["description"], raw_desc)
            check(r["short_title"] == fdc_short(raw_desc), r["source"], code,
                  "short_title", r["short_title"], fdc_short(raw_desc))
            exp_nut = fdc_expected_nutrients(dataset, {code}).get(code, {})
            check_nutrients(r["source"], code, r["nutrients"], exp_nut)
        elif fam == "bls":
            if bls_idx is None:
                if verbose:
                    print("  (loading BLS raw xlsx ...)")
                bls_idx = build_bls_index(source_dir)
            raw = bls_idx.get(code)
            if not raw:
                check(False, r["source"], code, "in-source", "missing", "present")
                continue
            exp_desc = raw["name_en"] or raw["name_de"]
            check(r["description"] == exp_desc, r["source"], code,
                  "description", r["description"], exp_desc)
            check(r["short_title"] == bls_short(exp_desc), r["source"], code,
                  "short_title", r["short_title"], bls_short(exp_desc))
            check_nutrients(r["source"], code, r["nutrients"], raw["nutrients"])
            if raw["name_de"]:
                check(r["de"] == raw["name_de"], r["source"], code,
                      "de_name", r["de"], raw["name_de"])
    return checks, fails, per_source, per_field


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("SUPABASE_DB_URL"),
                    help="Supabase connection string (default: $SUPABASE_DB_URL)")
    ap.add_argument("--csv-dir", type=Path, default=None,
                    help="validate converted CSVs offline instead of the DB")
    ap.add_argument("--source-dir", type=Path, default=Path("source"),
                    help="raw origin data folder (default: ./source)")
    ap.add_argument("--source", nargs="+", choices=["fdc", "bls"],
                    default=["fdc", "bls"], help="sources to sample (DB mode)")
    ap.add_argument("--samples", type=int, default=50)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log every individual check (pass and fail), not "
                         "just failures")
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
    if not args.source_dir.is_dir():
        sys.exit(f"error: --source-dir {args.source_dir} not found")

    if args.csv_dir:
        where = f"CSVs in {args.csv_dir}"
        rows = sample_from_csv(args.csv_dir, args.samples)
    else:
        if not args.db:
            sys.exit("error: no connection string - set $SUPABASE_DB_URL or "
                     "pass --db (or use --csv-dir for offline validation)")
        where = "Supabase"
        rows = sample_from_db(args.db, args.samples, args.source)
    print(f"sampled {len(rows)} foods from {where}; "
          f"validating against {args.source_dir} ...")
    if not rows:
        sys.exit("error: no rows sampled - is the data imported?")
    by_src = {}
    for r in rows:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    print("  sampled per source: "
          + ", ".join(f"{s}={n}" for s, n in sorted(by_src.items())))
    print("  checks per food: description, short_title, all 24 nutrients"
          " (+ de_name for BLS)")

    checks, fails, per_source, per_field = validate(
        rows, args.source_dir, args.verbose)

    print("\n--- per source ---")
    for s in sorted(per_source):
        c, f = per_source[s]
        print(f"  {s:16} {c - f:>4}/{c:<4} passed" + (f"  ({f} FAIL)" if f else ""))
    print("--- per field ---")
    for fld in sorted(per_field):
        c, f = per_field[fld]
        print(f"  {fld:14} {c - f:>4}/{c:<4} passed" + (f"  ({f} FAIL)" if f else ""))

    print(f"\n{checks - fails}/{checks} checks passed"
          f"{' - ALL GOOD' if not fails else f', {fails} FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
