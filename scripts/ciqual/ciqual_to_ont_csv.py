#!/usr/bin/env python3
"""Convert the ANSES CIQUAL food composition table (XML export) into CSV
files matching the multi-source Supabase schema
(multisource_food_schema.mermaid) - the same tables the BLS converter
fills.

Emitted CSVs:

    food_source.csv       (1 row: ciqual)
    nutrient_mapping.csv  (source='ciqual' rows; CIQUAL const_code -> canonical)
    food_category.csv     (CIQUAL sub-groups, ids 40_000 + sub-group code)
    food.csv              (ids 40_000_000 + alim_code; English names)
    food_translation.csv  (native French names, locale 'fr')
    food_alias.csv        (scientific names, locale 'la', source 'derived';
                           placeholders such as '(Genus and species
                           unknown or multiple)' are skipped)
    food_nutrient.csv     (canonical nutrients, ids from 50_000_000)

NOT emitted: nutrient.csv (shared canonical list, ships with the FDC
conversion), a component catalog (the schema has no generic one, and
per-source tables are not allowed - nutrient_mapping documents every
constituent used), food_portion / food_image (CIQUAL has neither).

Usage:
    # with the XML files auto-found under ./source/ciqual (download_ciqual.py):
    python ciqual_to_ont_csv.py --output <out_dir>
    python ciqual_to_ont_csv.py --source-dir /data/ciqual --output <out_dir>

Typical pipeline:
    python download_ciqual.py --output source           # -> ./source/ciqual
    python ciqual_to_ont_csv.py --output ../../out/ciqual

Standard library only (xml.etree).

Import after the FDC CSVs; append, don't truncate:
    python shared/import_fdc.py --csv-dir out/ciqual --append

Notes
-----
* food.id = 40_000_000 + alim_code (alim_code < 100_000), so ids are
  stable across CIQUAL releases and never collide with FDC (<10M),
  BLS (10M+), INDB (20M+) or TBCA (30M+). food_nutrient ids start at
  50_000_000; category ids are 40_000 + the 4-digit sub-group code
  (e.g. 0204 fruits -> 40204), or 40_000 + group*100 for foods filed
  directly under a group.
* Value quirks (compo 'teneur' column, comma decimals):
    '-'       missing                  -> no row (NULL; CIQUAL's docs
                                          insist missing is not zero)
    'traces'  detected, not quantifiable, or < 0.00001
                                       -> amount 0
    '< x'     below the limit of quantification x
                                       -> amount 0, min 0, max x (the
                                          bound is kept in food_nutrient)
  0 rather than NULL because these ARE measurements: the content is
  known to be negligible/at most x. This matches FDC, which reports
  below-LOQ results as 0. min/max otherwise come from CIQUAL's own
  min/max columns (range across its source data).
* Energy = const 328, kcal per EU Regulation 1169/2011 (protein N x 6.25,
  polyols/organic acids/fibre/alcohol included). Protein = 25003 crude
  protein N x 6.25, the definition EU labels and that energy figure use
  (and what BLS's PROT625 is); fallback 25000 (N x Jones factor).
* Vitamin A = const 51104, CIQUAL's vitamin A activity (INFOODS 'RAE',
  IOM 2001: retinol + beta-carotene/12 = ug RAE, the canonical unit).
  CIQUAL publishes it only when both retinol and beta-carotene are known;
  no fallback to retinol alone (it would understate plant foods), so it
  stays NULL otherwise.
* Vitamin D = 52100 (D2 + D3 as published by CIQUAL). Niacin = 56310,
  preformed niacin (no tryptophan equivalents).
* No trans-fat constituent in CIQUAL -> trans_fat_100 stays NULL.
* All CIQUAL units already match the canonical ones (g, mg, ug, kcal per
  100 g edible portion): every unit_factor is 1.
* origin: 'calculated' for energy (CIQUAL computes it from the
  macronutrients), empty (unknown) otherwise - CIQUAL's confidence codes
  (A-D) are a quality grade, not an origin. reference = the CIQUAL source
  citation behind the mean value (sources_*.xml).
* CIQUAL is published under the Licence Ouverte / Etalab 2.0. Attribution:
  "Anses. 2025. Table de composition nutritionnelle des aliments Ciqual"
  (https://doi.org/10.57745/RDMHWY).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

FOOD_ID_BASE = 40_000_000          # + alim_code
NUTRIENT_ID_BASE = 50_000_000      # food_nutrient ids
CATEGORY_ID_BASE = 40_000          # + sub-group code
DEFAULT_SOURCE_DIR = Path("source") / "ciqual"   # what download_ciqual.py fills

# ---------------------------------------------------------------------------
# Canonical nutrient ids (must match fdc_to_ont_csv.py) mapped to CIQUAL
# const_code values, in priority order (first value that is not '-' wins).
# unit_factor converts the CIQUAL unit to the canonical unit.
# ---------------------------------------------------------------------------
CIQUAL_MAPPING = [
    # (canonical id, [(const_code, unit_factor), ...])
    (1,  [("328", 1)]),               # energy, EU 1169/2011, kcal
    (2,  [("31000", 1)]),             # available carbohydrate, g
    (3,  [("40000", 1)]),             # fat, g
    (4,  [("25003", 1), ("25000", 1)]),  # protein N x 6.25, fallback Jones, g
    (5,  [("32000", 1)]),             # total sugars, g
    (6,  [("40302", 1)]),             # saturated FA, g
    (7,  [("34100", 1)]),             # dietary fibre, g
    (8,  [("40303", 1)]),             # monounsaturated FA, g
    (9,  [("40304", 1)]),             # polyunsaturated FA, g
    # 10: trans fat - no CIQUAL constituent, stays NULL
    (11, [("75100", 1)]),             # cholesterol, mg
    (12, [("10110", 1)]),             # sodium, mg
    (13, [("10190", 1)]),             # potassium, mg
    (14, [("10120", 1)]),             # magnesium, mg
    (15, [("10200", 1)]),             # calcium, mg
    (16, [("10260", 1)]),             # iron, mg
    (17, [("10300", 1)]),             # zinc, mg
    (18, [("10150", 1)]),             # phosphorus, mg
    (19, [("51104", 1)]),             # vitamin A activity, ug RAE
    (20, [("55100", 1)]),             # vitamin C, mg
    (21, [("52100", 1)]),             # vitamin D, ug
    (22, [("56500", 1)]),             # vitamin B6, mg
    (23, [("56600", 1)]),             # vitamin B12, ug
    (24, [("56310", 1)]),             # niacin (preformed), mg
]
CALCULATED_CONSTS = {"328"}            # energy is computed by CIQUAL

_LT_RE = re.compile(r"^<\s*(.+)$")


def short_title(description: str) -> str:
    """Concise display name: text before the first comma, capped at 80."""
    head = description.split(",")[0].strip()
    return (head or description.strip())[:80]


def to_float(s: str | None) -> float | None:
    """CIQUAL numbers use a decimal comma ('59,7'), sometimes '1E-6'."""
    s = (s or "").strip().replace(",", ".")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_teneur(teneur: str | None) -> tuple[float, float | None] | None:
    """CIQUAL 'teneur' -> (amount, upper_bound) or None when missing.

    '-' / empty -> None (missing, stays NULL); 'traces' -> (0, None);
    '< x' -> (0, x); a number -> (number, None).
    """
    s = (teneur or "").strip()
    if not s or s == "-":
        return None
    if s.lower() == "traces":
        return 0.0, None
    m = _LT_RE.match(s)
    if m:
        bound = to_float(m.group(1))
        return (0.0, bound) if bound is not None else None
    v = to_float(s)
    return (v, None) if v is not None else None


def text(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def records(path: Path, tag: str):
    """Yield {child_tag: text} per <tag> record, streaming (compo is ~70 MB)."""
    for _ev, el in ET.iterparse(path):
        if el.tag == tag:
            yield {c.tag: (c.text or "").strip() for c in el}
            el.clear()


def find_files(source_dir: Path) -> dict[str, Path]:
    """Newest alim/alim_grp/compo/const/sources XML under source_dir."""
    patterns = {
        "alim": re.compile(r"^alim_(\d{4}_\d{2}_\d{2})\.xml$", re.I),
        "alim_grp": re.compile(r"^alim_grp_(\d{4}_\d{2}_\d{2})\.xml$", re.I),
        "compo": re.compile(r"^compo_(\d{4}_\d{2}_\d{2})\.xml$", re.I),
        "const": re.compile(r"^const_(\d{4}_\d{2}_\d{2})\.xml$", re.I),
        "sources": re.compile(r"^sources_(\d{4}_\d{2}_\d{2})\.xml$", re.I),
    }
    found: dict[str, tuple[str, Path]] = {}
    for p in source_dir.rglob("*.xml"):
        for key, rx in patterns.items():
            m = rx.match(p.name)
            if m and (key not in found or m.group(1) > found[key][0]):
                found[key] = (m.group(1), p)
    missing = [k for k in ("alim", "alim_grp", "compo", "const") if k not in found]
    if missing:
        sys.exit(f"error: no CIQUAL {', '.join(k + '_*.xml' for k in missing)} "
                 f"under {source_dir} - run download_ciqual.py first, or pass "
                 f"--source-dir")
    return {k: v[1] for k, v in found.items()}


def release_date(path: Path) -> str:
    """'compo_2025_11_03.xml' -> '2025-11-03' (the export date)."""
    m = re.search(r"(\d{4})_(\d{2})_(\d{2})", path.name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def license_note(source_dir: Path, year: str) -> str:
    doi = "10.57745/RDMHWY"
    rel = source_dir / "ciqual_release.json"
    if rel.exists():
        try:
            doi = json.loads(rel.read_text(encoding="utf-8"))["doi"] \
                .removeprefix("doi:")
        except (ValueError, KeyError):
            pass
    return (f"Licence Ouverte / Etalab 2.0 - Anses. {year}. Table de "
            f"composition nutritionnelle des aliments Ciqual {year}. "
            f"https://doi.org/{doi}")


def category_of(grp: str, ssgrp: str) -> int | None:
    """Sub-group '0204' -> 40204; a food filed directly under group '08'
    (sub-group '0000') -> 40800; unclassified ('00') -> None."""
    if ssgrp.isdigit() and int(ssgrp) > 0:
        return CATEGORY_ID_BASE + int(ssgrp)
    if grp.isdigit() and int(grp) > 0:
        return CATEGORY_ID_BASE + int(grp) * 100
    return None


def convert(source_dir: Path, out_dir: Path) -> None:
    files = find_files(source_dir)
    for k in ("alim", "alim_grp", "compo", "const", "sources"):
        if k in files:
            print(f"{k + ':':10s} {files[k]}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- constituent catalog: check every mapped code exists --------------
    consts = {r["const_code"]: r for r in records(files["const"], "CONST")}
    for nid, options in CIQUAL_MAPPING:
        for code, _f in options:
            if code not in consts:
                print(f"  note: CIQUAL const {code} (canonical {nid}) not in "
                      f"this release", file=sys.stderr)

    # ---- groups -> categories ---------------------------------------------
    cat_names: dict[int, str] = {}
    for r in records(files["alim_grp"], "ALIM_GRP"):
        grp, ssgrp = r.get("alim_grp_code", ""), r.get("alim_ssgrp_code", "")
        cid = category_of(grp, ssgrp)
        if cid is None or cid in cat_names:
            continue
        name = r.get("alim_ssgrp_nom_eng", "")
        if cid % 100 == 0 or not name or name == "-":
            name = r.get("alim_grp_nom_eng", "")
        cat_names[cid] = name[:1].upper() + name[1:]

    # ---- citations ----------------------------------------------------------
    citations = {}
    if "sources" in files:
        citations = {r["source_code"]: r.get("ref_citation", "")
                     for r in records(files["sources"], "SOURCES")}

    # ---- foods --------------------------------------------------------------
    pub_date = release_date(files["compo"])
    foods, translations, aliases = [], [], []
    food_id_of: dict[str, int] = {}
    used_cats: set[int] = set()
    for r in records(files["alim"], "ALIM"):
        code = r.get("alim_code", "")
        name_en = r.get("alim_nom_eng", "")
        name_fr = r.get("alim_nom_fr", "")
        if not code.isdigit() or code in food_id_of or not (name_en or name_fr):
            continue
        food_id = FOOD_ID_BASE + int(code)
        food_id_of[code] = food_id
        cid = category_of(r.get("alim_grp_code", ""), r.get("alim_ssgrp_code", ""))
        if cid is not None and cid not in cat_names:
            cid = None                           # group code without a name
        if cid is not None:
            used_cats.add(cid)
        description = name_en or name_fr        # English; FR fallback
        foods.append({
            "id": food_id,
            "source": "ciqual",
            "source_code": code,
            "description": description,
            "short_title": short_title(description),
            "food_category_id": "" if cid is None else cid,
            "publication_date": pub_date,
        })
        if name_fr:
            translations.append({"food_id": food_id, "locale": "fr",
                                 "description": name_fr, "source": "native"})
        sci = r.get("alim_nom_sci", "")
        # skip placeholders like '(Genus and species unknown or multiple)'
        if sci and sci != "-" and not sci.startswith("("):
            aliases.append({"food_id": food_id, "locale": "la",
                            "alias": sci, "source": "derived"})

    # ---- composition --------------------------------------------------------
    # const_code -> (canonical id, priority, factor)
    wanted = {code: (nid, prio, factor)
              for nid, options in CIQUAL_MAPPING
              for prio, (code, factor) in enumerate(options)}
    best: dict[tuple[int, int], tuple[int, dict]] = {}
    unknown_foods: set[str] = set()
    for r in records(files["compo"], "COMPO"):
        hit = wanted.get(r.get("const_code", ""))
        if not hit:
            continue
        alim = r.get("alim_code", "")
        food_id = food_id_of.get(alim)
        if food_id is None:
            unknown_foods.add(alim)
            continue
        parsed = parse_teneur(r.get("teneur"))
        if parsed is None:
            continue                             # '-' -> NULL
        nid, prio, factor = hit
        key = (food_id, nid)
        if key in best and best[key][0] <= prio:
            continue
        amount, upper = parsed
        lo, hi = to_float(r.get("min")), to_float(r.get("max"))
        if upper is not None:                    # '< x': 0, bounded by x
            lo = 0.0 if lo is None else lo
            hi = upper if hi is None else hi
        const = r.get("const_code", "")
        src = r.get("source_code", "")
        best[key] = (prio, {
            "food_id": food_id,
            "nutrient_id": nid,
            "amount": round(amount * factor, 6),
            "origin": "calculated" if const in CALCULATED_CONSTS else "",
            "reference": citations.get(src, "") or "",
            "data_points": "",
            "min": "" if lo is None else round(lo * factor, 6),
            "max": "" if hi is None else round(hi * factor, 6),
        })
    if unknown_foods:
        print(f"  note: {len(unknown_foods)} compo alim_codes not in alim file "
              f"(skipped): {sorted(unknown_foods)[:8]}", file=sys.stderr)

    food_nutrients = []
    for i, (_k, (_p, row)) in enumerate(sorted(best.items())):
        food_nutrients.append({"id": NUTRIENT_ID_BASE + i, **row})

    # ------------------------------------------------------------------ write
    def write(name: str, rows_, columns: list[str]) -> None:
        with open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_)
        print(f"  {name:20s} {len(rows_):>7,} rows")

    year = pub_date[:4] or "2025"
    print("Writing Supabase CSVs:")
    write("food_source",
          [{"code": "ciqual",
            "name": f"Table de composition nutritionnelle des aliments "
                    f"Ciqual {year} (Anses)",
            "license_note": license_note(source_dir, year)}],
          ["code", "name", "license_note"])
    write("nutrient_mapping",
          [{"source": "ciqual", "source_component_code": c,
            "nutrient_id": nid, "unit_factor": f}
           for nid, opts in CIQUAL_MAPPING for c, f in opts],
          ["source", "source_component_code", "nutrient_id", "unit_factor"])
    write("food_category",
          [{"id": cid, "source": "ciqual", "description": cat_names[cid]}
           for cid in sorted(used_cats)],
          ["id", "source", "description"])
    write("food", foods,
          ["id", "source", "source_code", "description", "short_title",
           "food_category_id", "publication_date"])
    write("food_translation", translations,
          ["food_id", "locale", "description", "source"])
    write("food_alias", aliases, ["food_id", "locale", "alias", "source"])
    write("food_nutrient", food_nutrients,
          ["id", "food_id", "nutrient_id", "amount", "origin",
           "reference", "data_points", "min", "max"])

    n_energy = len({r["food_id"] for r in food_nutrients if r["nutrient_id"] == 1})
    print(f"\nDone. {len(foods):,} CIQUAL foods ({n_energy:,} with energy, "
          f"{len(used_cats)} categories) -> {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert the ANSES CIQUAL XML tables to multi-source "
                    "Supabase CSVs")
    ap.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR,
                    help="folder from download_ciqual.py holding the XML "
                         "files (default: ./source/ciqual)")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    if not args.source_dir.is_dir():
        sys.exit(f"error: {args.source_dir} not found - run "
                 f"download_ciqual.py first, or pass --source-dir")
    convert(args.source_dir, args.output)


if __name__ == "__main__":
    main()
