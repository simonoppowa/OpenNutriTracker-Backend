#!/usr/bin/env python3
"""Validate imported food data against the ORIGINAL source files.

Samples random foods from the database (or from converted CSVs) and, for
each, re-derives the expected values straight from the raw source
downloads (FDC, BLS, CIQUAL, Fineli, Frida, Matvaretabellen, SLV) -
independently of the converters - then compares. Catches import bugs,
unit-conversion drift, and truncation that a converter self-test would
miss.

Per sampled food it checks:
    * description        == raw source name
    * short_title        == first comma-segment of the raw name (BLS/FDC)
    * all 24 canonical nutrients (energy, macros, minerals, vitamins)
      in food_summary == the raw values, resolved with the same
      per-nutrient priority + unit factors the converter uses (NULL-aware,
      within a 2% tolerance for import rounding)
    * native-name food_translation rows (source 'native') == the raw
      names:
        - BLS: German 'de' == raw 'Lebensmittelbezeichnung'
        - CIQUAL: French 'fr' == raw 'alim_nom_fr'
        - Fineli: Finnish 'fi' / Swedish 'sv' == raw foodname_FI /
          foodname_SV (sentence-cased like the converter)
        - Frida: Danish 'da' == raw 'FødevareNavn'
        - MVT: Bokmaal 'nb' == raw nb-foods.json name
        - SLV: Swedish 'sv' == raw Swedish name (sprak=1)
    * Frida nutrients are re-derived from the wide 'Data_Table' sheet (the
      converter reads the long 'Data_Normalised' sheet), vitamin A RAE
      recomputed here

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

    # offline: CIQUAL CSVs vs the raw XML in ./source/ciqual:
    python test_against_source.py --csv-dir out/ciqual --source ciqual

    # offline: validate the Fineli CSVs against ./source/fineli:
    python test_against_source.py --csv-dir out/fineli --source-dir source \
        --source fineli --samples 100

    # offline: validate the Frida CSVs against ./source/frida:
    python test_against_source.py --csv-dir out/frida --source-dir source \
        --source frida --samples 100

    # Matvaretabellen (raw JSON under source/mvt):
    python test_against_source.py --csv-dir out/mvt --source-dir source \
        --source mvt --samples 200

    # offline: validate the Livsmedelsverket (SLV) CSVs:
    python test_against_source.py --csv-dir out/slv --source-dir source \
        --source slv --samples 100

Requires: psycopg2-binary (for --db), openpyxl (for BLS / Frida).
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
sys.path.insert(0, str(HERE / "ciqual"))
sys.path.insert(0, str(HERE / "fineli"))
sys.path.insert(0, str(HERE / "frida"))
sys.path.insert(0, str(HERE / "mvt"))
sys.path.insert(0, str(HERE / "slv"))
sys.path.insert(0, str(HERE / "shared"))

from fdc_to_ont_csv import (CANONICAL_NUTRIENTS, SOURCE_BY_DATA_TYPE,  # noqa: E402
                            short_title as fdc_short)
from bls_to_ont_csv import (BLS_MAPPING, parse_amount,                 # noqa: E402
                            short_title as bls_short)
from ciqual_to_ont_csv import (CIQUAL_MAPPING,                         # noqa: E402
                               short_title as ciqual_short)
from fineli_to_ont_csv import (FINELI_MAPPING,                         # noqa: E402
                               parse_amount as fineli_amount,
                               sentence_case as fineli_case,
                               short_title as fineli_short)
from frida_to_ont_csv import (FRIDA_MAPPING,                           # noqa: E402
                              short_title as frida_short)
from mvt_to_ont_csv import (MVT_MAPPING, norm_unit as mvt_norm_unit,   # noqa: E402
                            short_title as mvt_short)
from slv_to_ont_csv import (SLV_MAPPING,                               # noqa: E402
                            parse_amount as slv_amount,
                            short_title as slv_short)

TOL = 0.02          # 2% relative tolerance for floats (rounding at import)
# --source choices (DB mode)
SOURCES = ["fdc", "bls", "ciqual", "fineli", "frida", "mvt", "slv"]

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


def source_subdir(source_dir: Path, name: str) -> Path:
    """<source_dir>/<name> when it exists, else source_dir itself (so
    --source-dir may point at either the shared root or the source's own
    folder)."""
    sub = source_dir / name
    return sub if sub.is_dir() else source_dir


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


# ----------------------------------------------------------------- CIQUAL origin
def ciqual_raw_amount(teneur: str):
    """Independent re-read of a CIQUAL 'teneur': '-' -> None,
    'traces' / '< x' -> 0.0 (the converter's documented policy),
    '12,5' -> 12.5."""
    s = (teneur or "").strip()
    if not s or s == "-":
        return None
    if s == "traces" or s.startswith("<"):
        return 0.0
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def build_ciqual_index(source_dir: Path):
    """alim_code -> {name_fr, name_en, nutrients:{canonical_id: amount}}
    from the raw CIQUAL XML (alim_*.xml + compo_*.xml), applying
    CIQUAL_MAPPING's priority and unit factors."""
    import xml.etree.ElementTree as ET
    base = source_subdir(source_dir, "ciqual")
    alims = sorted(p for p in base.rglob("alim_*.xml")
                   if not p.name.startswith("alim_grp_"))
    compos = sorted(base.rglob("compo_*.xml"))
    if not alims or not compos:
        return {}
    idx = {}
    for _ev, el in ET.iterparse(alims[-1]):
        if el.tag == "ALIM":
            code = (el.findtext("alim_code") or "").strip()
            idx[code] = {"name_fr": (el.findtext("alim_nom_fr") or "").strip(),
                         "name_en": (el.findtext("alim_nom_eng") or "").strip(),
                         "nutrients": {}, "_prio": {}}
            el.clear()
    wanted = {c: (nid, p, f) for nid, opts in CIQUAL_MAPPING
              for p, (c, f) in enumerate(opts)}
    for _ev, el in ET.iterparse(compos[-1]):
        if el.tag != "COMPO":
            continue
        hit = wanted.get((el.findtext("const_code") or "").strip())
        food = idx.get((el.findtext("alim_code") or "").strip())
        amount = ciqual_raw_amount(el.findtext("teneur")) if hit and food \
            else None
        if amount is not None:
            nid, prio, factor = hit
            if prio < food["_prio"].get(nid, 99):
                food["_prio"][nid] = prio
                food["nutrients"][nid] = round(amount * factor, 6)
        el.clear()
    return idx


# ----------------------------------------------------------------- Fineli origin
def _fineli_rows(fineli_dir: Path, name: str) -> list[dict]:
    """One semicolon-separated Fineli file (UTF-8, else Latin-1/cp1252)."""
    hits = [p for p in fineli_dir.iterdir() if p.name.lower() == name.lower()]
    if not hits:
        return []
    raw = hits[0].read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", "replace")
    return list(csv.DictReader(text.splitlines(), delimiter=";"))


def build_fineli_index(source_dir: Path):
    """FOODID -> {name_en, name_fi, name_sv, nutrients:{canonical_id:
    amount}}. Applies FINELI_MAPPING (EUFDNAME + unit_factor, per-nutrient
    priority) to the raw component_value.csv - the converter's mapping,
    re-read from the raw files."""
    d = source_subdir(source_dir, "fineli")
    names = {lang: {r["FOODID"]: r["FOODNAME"].strip()
                    for r in _fineli_rows(d, f"foodname_{lang}.csv")}
             for lang in ("EN", "FI", "SV")}
    raw: dict[str, dict[str, float]] = {}
    for r in _fineli_rows(d, "component_value.csv"):
        amount = fineli_amount(r["BESTLOC"])
        if amount is not None:
            raw.setdefault(r["FOODID"], {})[r["EUFDNAME"]] = amount
    idx = {}
    for r in _fineli_rows(d, "food.csv"):
        fid = r["FOODID"]
        vals = raw.get(fid, {})
        nutrients = {}
        for nid, options in FINELI_MAPPING:
            for code, factor in options:          # first present wins
                if code in vals:
                    nutrients[nid] = round(vals[code] * factor, 6)
                    break
        idx[fid] = {
            "name_en": names["EN"].get(fid) or r["FOODNAME"].strip(),
            "name_fi": names["FI"].get(fid, ""),
            "name_sv": names["SV"].get(fid, ""),
            "nutrients": nutrients,
        }
    return idx


# ----------------------------------------------------------------- Frida origin
def build_frida_index(source_dir: Path):
    """Frida FoodID -> {name_en, name_da, nutrients:{canonical_id: amount}}
    from the wide 'Data_Table' sheet (header rows: Danish name, English
    name, unit, ParameterID; then one row per food), applying FRIDA_MAPPING
    priority + unit factors. Vitamin A RAE is recomputed from Frida's RE
    (12) = retinol (225) + beta-carotene (303)/6 and RAE = retinol +
    beta-carotene/12, independently of the converter."""
    try:
        import openpyxl
    except ImportError:
        sys.exit("error: openpyxl needed for Frida validation")
    hits = sorted(p for p in (source_dir / "frida").rglob("*.xlsx")
                  if "dataset" in p.name.lower())
    if not hits:
        return {}
    wb = openpyxl.load_workbook(hits[-1], read_only=True)
    rows = list(wb["Data_Table"].iter_rows(values_only=True))
    wb.close()
    hdr = next(i for i, r in enumerate(rows)
               if str(r[2] or "").startswith("\u2193FoodID"))
    col = {str(p): i for i, p in enumerate(rows[hdr]) if isinstance(p, int)}

    def get(row, pid):
        i = col.get(pid)
        v = row[i] if i is not None else None
        return float(v) if isinstance(v, (int, float)) else None

    idx = {}
    for row in rows[hdr + 1:]:
        if not isinstance(row[2], int):
            continue
        nutrients = {}
        for nid, options in FRIDA_MAPPING:
            for pid, factor, _unit in options:
                v = get(row, pid)
                if v is not None:
                    nutrients[nid] = round(v * factor, 6)
                    break
        ret, bc, re_ = get(row, "225"), get(row, "303"), get(row, "12")
        if ret is None and re_ is not None and bc is not None:
            ret = re_ - bc / 6                    # RE = retinol + bc/6
        if bc is None and re_ is not None and ret is not None:
            bc = (re_ - ret) * 6
        if ret is not None and bc is not None:
            nutrients[19] = round(max(ret + bc / 12, 0.0), 6)
        idx[str(row[2])] = {"name_da": str(row[0] or "").strip(),
                            "name_en": str(row[1] or "").strip(),
                            "nutrients": nutrients}
    return idx


# ----------------------------------------------------------------- MVT origin
def build_mvt_index(source_dir: Path):
    """MVT foodId -> {name_en, name_nb, nutrients:{canonical_id: amount}}.
    Reads the raw en/nb JSON exports and applies MVT_MAPPING (code,
    expected unit, unit_factor, per-nutrient priority) to each food."""
    import json
    base = source_subdir(source_dir, "mvt")
    en_path, nb_path = base / "en-foods.json", base / "nb-foods.json"
    if not en_path.exists():
        return {}

    def foods(path: Path) -> list:
        return json.loads(path.read_text(encoding="utf-8"))["foods"] \
            if path.exists() else []

    nb_names = {str(f.get("foodId") or "").strip():
                str(f.get("foodName") or "").strip() for f in foods(nb_path)}
    idx = {}
    for f in foods(en_path):
        code = str(f.get("foodId") or "").strip()
        raw = {c.get("nutrientId"): c for c in f.get("constituents", [])}
        raw["calories"] = f.get("calories") or {}
        raw["energy"] = f.get("energy") or {}
        nutrients = {}
        for nid, options in MVT_MAPPING:
            for mvt_code, unit, factor in options:   # first present wins
                c = raw.get(mvt_code) or {}
                q = c.get("quantity")
                if q is None or mvt_norm_unit(c.get("unit")) != mvt_norm_unit(unit):
                    continue
                nutrients[nid] = round(float(q) * factor, 6)
                break
        idx[code] = {
            "name_en": str(f.get("foodName") or "").strip(),
            "name_nb": nb_names.get(code, ""),
            "nutrients": nutrients,
        }
    return idx


# ----------------------------------------------------------------- SLV origin
def build_slv_index(source_dir: Path, codes: set[str]):
    """SLV nummer -> {name_en, name_sv, nutrients:{canonical_id: amount}}
    for the sampled codes, read from download_slv.py's cached JSON.
    Re-applies SLV_MAPPING (EuroFIR code + unit, per-nutrient priority,
    unit factor) independently of the converter."""
    import json
    base = source_dir / "slv"
    names = {}
    for lang in ("en", "sv"):
        with open(base / f"livsmedel_{lang}.json", encoding="utf-8") as fh:
            for f in json.load(fh):
                names.setdefault(str(f["nummer"]), {})[lang] = \
                    (f.get("namn") or "").strip()
    idx = {}
    for code in codes:
        if code not in names:
            continue
        path = base / "naringsvarden" / f"{code}.json"
        rows = json.loads(path.read_text(encoding="utf-8")) \
            if path.exists() else []
        values = {}
        for r in rows:
            values.setdefault((r.get("euroFIRkod"), r.get("enhet")),
                              r.get("varde"))
        nutrients = {}
        for nid, options in SLV_MAPPING:
            for comp, unit, factor in options:     # first present wins
                amount = slv_amount(values.get((comp, unit)))
                if amount is not None and amount >= 0:
                    nutrients[nid] = round(amount * factor, 6)
                    break
        idx[code] = {"name_en": names[code].get("en", ""),
                     "name_sv": names[code].get("sv", ""),
                     "nutrients": nutrients}
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
                   (select json_object_agg(t.locale, t.description)
                    from food_translation t where t.food_id = f.id
                      and t.source = 'native') as native,
                   {ncols}
            from food f join food_summary s on s.food_id = f.id
            where split_part(f.source,'_',1) = any(%s)
            order by random() limit %s""", (list(sources), n))
        cols = [c[0] for c in cur.description]
        rows = []
        for raw in cur.fetchall():
            d = dict(zip(cols, raw))
            d["native"] = d["native"] or {}       # {locale: native name}
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
    native = {}                                # food_id -> {locale: name}
    ft = csv_dir / "food_translation.csv"
    if ft.exists():
        for r in read_csv(ft):
            if r.get("source") == "native":
                native.setdefault(r["food_id"], {})[r["locale"]] = \
                    r["description"]
    picks = random.sample(foods, min(n, len(foods)))
    out = []
    for r in picks:
        vals = nut.get(r["id"], {})
        out.append({
            "source": r["source"], "source_code": r["source_code"],
            "description": r["description"], "short_title": r["short_title"],
            "native": native.get(r["id"], {}),
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
    if "ciqual" in fams and not list(source_dir.rglob("compo_*.xml")):
        sys.exit(f"error: no CIQUAL compo_*.xml under {source_dir}/ciqual - "
                 f"wrong --source-dir? run download_ciqual.py first.")
    if "fineli" in fams and not (
            list(source_dir.glob("fineli/component_value.csv"))
            or list(source_dir.glob("component_value.csv"))):
        sys.exit(f"error: no Fineli component_value.csv under "
                 f"{source_dir}/fineli - wrong --source-dir? run "
                 f"download_fineli.py first.")
    if "frida" in fams and not any(
            "dataset" in p.name.lower()
            for p in (source_dir / "frida").rglob("*.xlsx")):
        sys.exit(f"error: no Frida *Dataset*.xlsx under {source_dir}/frida - "
                 f"wrong --source-dir? run download_frida.py first.")
    if "mvt" in fams and not ((source_dir / "mvt" / "en-foods.json").exists()
                              or (source_dir / "en-foods.json").exists()):
        sys.exit(f"error: no Matvaretabellen en-foods.json under "
                 f"{source_dir}/mvt - wrong --source-dir? run "
                 f"download_mvt.py first.")
    if "slv" in fams and not (source_dir / "slv" / "livsmedel_en.json").exists():
        sys.exit(f"error: no SLV livsmedel_en.json under {source_dir}/slv - "
                 f"wrong --source-dir? run download_slv.py first.")


def validate(rows, source_dir: Path, verbose: bool):
    preflight(rows, source_dir)
    fdc_idx = None
    bls_idx = None
    ciqual_idx = None
    fineli_idx = None
    frida_idx = None
    mvt_idx = None
    slv_idx = None
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

    def check_native(source, code, native, expected):
        """Compare native-name translations, {locale: raw name}; a locale
        the raw source leaves blank is not checked."""
        for loc, exp in expected.items():
            if exp:
                check(native.get(loc) == exp, source, code, f"{loc}_name",
                      native.get(loc), exp)

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
            check_native(r["source"], code, r["native"],
                         {"de": raw["name_de"]})
        elif fam == "ciqual":
            if ciqual_idx is None:
                if verbose:
                    print("  (loading CIQUAL raw XML ...)")
                ciqual_idx = build_ciqual_index(source_dir)
            raw = ciqual_idx.get(code)
            if not raw:
                check(False, r["source"], code, "in-source", "missing", "present")
                continue
            exp_desc = raw["name_en"] or raw["name_fr"]
            check(r["description"] == exp_desc, r["source"], code,
                  "description", r["description"], exp_desc)
            check(r["short_title"] == ciqual_short(exp_desc), r["source"], code,
                  "short_title", r["short_title"], ciqual_short(exp_desc))
            check_nutrients(r["source"], code, r["nutrients"], raw["nutrients"])
            check_native(r["source"], code, r["native"],
                         {"fr": raw["name_fr"]})
        elif fam == "fineli":
            if fineli_idx is None:
                if verbose:
                    print("  (loading Fineli raw CSVs ...)")
                fineli_idx = build_fineli_index(source_dir)
            raw = fineli_idx.get(code)
            if not raw:
                check(False, r["source"], code, "in-source", "missing", "present")
                continue
            exp_desc = fineli_case(raw["name_en"])
            check(r["description"] == exp_desc, r["source"], code,
                  "description", r["description"], exp_desc)
            check(r["short_title"] == fineli_short(exp_desc), r["source"], code,
                  "short_title", r["short_title"], fineli_short(exp_desc))
            check_nutrients(r["source"], code, r["nutrients"], raw["nutrients"])
            check_native(r["source"], code, r["native"],
                         {loc: fineli_case(raw[f"name_{loc}"])
                          for loc in ("fi", "sv") if raw[f"name_{loc}"]})
        elif fam == "frida":
            if frida_idx is None:
                if verbose:
                    print("  (loading Frida raw xlsx ...)")
                frida_idx = build_frida_index(source_dir)
            raw = frida_idx.get(code)
            if not raw:
                check(False, r["source"], code, "in-source", "missing", "present")
                continue
            exp_desc = raw["name_en"] or raw["name_da"]
            check(r["description"] == exp_desc, r["source"], code,
                  "description", r["description"], exp_desc)
            check(r["short_title"] == frida_short(exp_desc), r["source"], code,
                  "short_title", r["short_title"], frida_short(exp_desc))
            check_nutrients(r["source"], code, r["nutrients"], raw["nutrients"])
            check_native(r["source"], code, r["native"],
                         {"da": raw["name_da"]})
        elif fam == "mvt":
            if mvt_idx is None:
                if verbose:
                    print("  (loading Matvaretabellen raw JSON ...)")
                mvt_idx = build_mvt_index(source_dir)
            raw = mvt_idx.get(code)
            if not raw:
                check(False, r["source"], code, "in-source", "missing", "present")
                continue
            check(r["description"] == raw["name_en"], r["source"], code,
                  "description", r["description"], raw["name_en"])
            check(r["short_title"] == mvt_short(raw["name_en"]), r["source"],
                  code, "short_title", r["short_title"],
                  mvt_short(raw["name_en"]))
            check_nutrients(r["source"], code, r["nutrients"], raw["nutrients"])
            check_native(r["source"], code, r["native"],
                         {"nb": raw["name_nb"]})
        elif fam == "slv":
            if slv_idx is None:
                slv_idx = build_slv_index(
                    source_dir, {x["source_code"] for x in rows
                                 if x["source"] == "slv"})
            raw = slv_idx.get(code)
            if not raw:
                check(False, r["source"], code, "in-source", "missing", "present")
                continue
            exp_desc = raw["name_en"] or raw["name_sv"]
            check(r["description"] == exp_desc, r["source"], code,
                  "description", r["description"], exp_desc)
            check(r["short_title"] == slv_short(exp_desc), r["source"], code,
                  "short_title", r["short_title"], slv_short(exp_desc))
            check_nutrients(r["source"], code, r["nutrients"], raw["nutrients"])
            check_native(r["source"], code, r["native"],
                         {"sv": raw["name_sv"]})
    return checks, fails, per_source, per_field


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("SUPABASE_DB_URL"),
                    help="Supabase connection string (default: $SUPABASE_DB_URL)")
    ap.add_argument("--csv-dir", type=Path, default=None,
                    help="validate converted CSVs offline instead of the DB")
    ap.add_argument("--source-dir", type=Path, default=Path("source"),
                    help="raw origin data folder (default: ./source)")
    ap.add_argument("--source", nargs="+", choices=SOURCES,
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
          " (+ native names: de for BLS, fr for CIQUAL, fi/sv for Fineli,"
          " da for Frida, nb for MVT, sv for SLV)")

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
