# AGENTS.md

Guidance for AI coding agents working in this repository. Read this before
making changes.

## What this project is

A Supabase backend that ingests food reference data from several national
databases (USDA FDC, German BLS, Indian INDB, Brazilian TBCA, French CIQUAL,
Finnish Fineli, Danish Frida) into one canonical schema, consumed by the
Flutter app [OpenNutriTracker](https://github.com/simonoppowa/OpenNutriTracker).
Each source is downloaded, converted to a shared set of CSVs, and
bulk-loaded. The app reads only the `food_summary` materialized view plus
`food_translation`.

## Architecture invariants (do not break)

- **One canonical schema, never per-source tables.** Adding a source means
  new *rows* in the shared tables (`food`, `food_nutrient`, `food_portion`,
  `food_translation`, `food_alias`, ...) plus a new converter script — never
  a new set of tables.
- **The app queries only `food_summary`** (a materialized view pivoting the
  24 canonical nutrients + default serving + image URLs + tags) and
  `food_translation`. Everything else is import-time only.
- **Exactly 24 canonical nutrients**, ids 1–24, matching the app's
  `MealNutrimentsDBO` (1 = energy_kcal ... 24 = niacin). The list lives in
  `scripts/fdc/fdc_to_ont_csv.py` (`CANONICAL_NUTRIENTS`); every converter
  maps its source's components onto it.
- **`nutrient_mapping.source` is an import FAMILY** (`fdc`, `bls`, `indb`,
  `tbca`, `ciqual`, `fineli`, `frida`), NOT a `food_source.code`, and has no
  FK — `fdc` covers `fdc_foundation` / `fdc_sr_legacy` / `fdc_survey` /
  `fdc_branded`.

## Fixed conventions

- **`food.id` ranges** (avoid collisions): FDC = raw `fdc_id` (<10M),
  BLS 10M+, INDB 20M+, TBCA 30M+, CIQUAL 40M + alim_code, Fineli 50M+,
  Frida 60M + FoodID (each new source takes its own free 10M block).
  `food_nutrient` id bases: BLS 20M+, INDB 30M+, TBCA 40M+, CIQUAL 50M+,
  Fineli 60M+, Frida 70M+.
  `food_portion` id bases: INDB 20M+, Fineli 50M+.
- **Category ids**: FDC 1–28, WWEIA/FNDDS 1002–9999, TBCA 30001+,
  CIQUAL 40000 + sub-group code (40101–41104), Fineli 50001+ (food-use
  classes, `source='fineli'`), Frida 60000 + FoodGroupID.
- **`food_nutrient.origin`** vocabulary: `analysis | literature |
  calculated | label` (empty when unknown).
- **i18n**: `food.description` is always **English**. Translations live in
  `food_translation (food_id, locale, description, source, ai_generated)`
  with `source ∈ native | machine | community | verified`. Native names
  (BLS German, CIQUAL French, Fineli Finnish/Swedish, Frida Danish) are
  never overwritten by machine translation; MT/LLM output sets
  `ai_generated = true`.
- **Nullable = honest**: a missing nutrient stays `NULL` (maps to dart
  `double?`), never `0`. Known gaps to respect: BLS has no trans fat; INDB
  has no trans fat and no B12; TBCA has no total sugars (only *added*
  sugar — do NOT map it to canonical sugars); CIQUAL has no trans fat,
  and vitamin A (µg RAE) only where CIQUAL publishes its retinol-activity
  value (~55% of foods; no retinol-only fallback). CIQUAL `traces` and
  `< x` (below quantification limit) are measurements, stored as 0 with
  `max = x`; only `-` is missing (NULL). Fineli covers all 24 (trans
  fat included); a few foods lack single components (e.g. 6 spices/water
  ship no values at all) and stay NULL. Frida covers all 24 but ships
  vitamin A as RE (retinol + β-carotene/6): the converter derives µg RAE
  from any two of RE/retinol/β-carotene and leaves RE-only foods NULL;
  vitamin D includes 25-OH-D ×2.5; niacin is preformed (not NE).
- **Unit discipline**: converters normalize to canonical units at import
  and document every factor in `nutrient_mapping` (e.g. BLS B6 µg→mg
  ×0.001; INDB SFA/MUFA/PUFA mg→g ×0.001; INDB vitamin D = D2 + D3;
  Fineli energy kJ→kcal ×1/4.184 — the package has no kcal component;
  Frida kJ→kcal ×1/4.184 fallback only).

## Repo map

```
sql/
  schema.sql        tables, food_summary view, RLS, indexes, storage bucket
  import_fdc.sql    psql \copy loaders (client-side; never the SQL Editor)
  import_bls.sql
scripts/
  fdc/  download_fdc.py, fdc_to_ont_csv.py   (Foundation/SR/FNDDS/Branded)
  bls/  download_bls.py, bls_to_ont_csv.py   (BLS 4.0 xlsx)
  indb/ indb_to_ont_csv.py                    (Anuvaad INDB xlsx)
  tbca/ tbca_to_ont_csv.py                    (scraped TBCA CSVs)
  ciqual/ download_ciqual.py, ciqual_to_ont_csv.py (ANSES CIQUAL XML)
  fineli/ download_fineli.py, fineli_to_ont_csv.py  (Fineli CSV zip)
  frida/ download_frida.py, frida_to_ont_csv.py (Danish FCDB xlsx, DTU Data)
  shared/ import_fdc.py         psycopg2 COPY loader (all sources)
          translate_all.py      DeepL, DB-driven, all sources, per-source CSVs
  run_pipeline.py               orchestrates download/convert/schema/import
  test_against_source.py        validates DB/CSVs vs raw source data
diagrams/multisource_food_schema.mermaid   schema ground truth
```

## How the pipeline flows

`download_* -> *_to_ont_csv (raw -> canonical CSVs in out/<source>) ->
import_fdc.py (COPY into Supabase, refresh food_summary)`. Converters read
`./source` (or `./source/bls`) by default. `run_pipeline.py` chains it all;
FDC must import before BLS (BLS reuses FDC's shared `nutrient` table), and
the first import truncates while the rest use `--append`.

## Running & testing (an agent MUST verify changes)

- **Python**: `python3 -m py_compile scripts/**/*.py` — everything must
  compile.
- **SQL**: validate with `pglast` (Postgres's own parser), stripping `\`
  meta-lines from the `import_*.sql` files first. No live Postgres is
  available in most sandboxes.
- **Data correctness**: `scripts/test_against_source.py` samples random
  foods and re-derives description, short_title, and all 24 nutrients from
  the raw source files — run it in `--csv-dir` mode offline, or against the
  DB with `$SUPABASE_DB_URL`. It exits non-zero on any mismatch.
- **Converters are tested on REAL data** before delivery; spot-check known
  values (Oat flakes BLS C133000 = 348 kcal; hummus FDC 321358 magnesium
  71.1) rather than trusting the code path.

## When you change the schema

Keep the trio in sync in the same change: `diagrams/
multisource_food_schema.mermaid` + `sql/schema.sql` + every affected
converter/importer. State the one-line migration for the live DB, and
remember `refresh materialized view food_summary;` after data changes.
`sql/schema.sql` must still parse with `pglast`.

## Security & credentials

- Never put a DB password or DeepL key on a subprocess command line or in a
  printed/logged string — pass them via env (`SUPABASE_DB_URL`,
  `DEEPL_API_KEY`). `import_fdc.py:normalize_db_url` percent-encodes raw
  passwords; reuse it.
- Use the **session pooler (port 5432)** — the transaction pooler (6543)
  cannot `COPY`.
- No `COPY FROM PROGRAM`, no `shell=True`, no `eval`/`pickle`. Guard zip
  extraction against path traversal (see `extract_and_locate`).

## Data & licensing

Raw downloads and converted CSVs are **gitignored** (code-only repo,
regenerated by the scripts). Licenses: FDC = CC0; BLS 4.0 = CC BY 4.0
(attribute Max Rubner-Institut); INDB = CC BY 4.0 (attribute
anuvaad.org.in); TBCA = free with attribution (USP/FoRC); CIQUAL = Licence
Ouverte / Etalab 2.0 (cite "Anses. 2025. Table de composition nutritionnelle
des aliments Ciqual", DOI 10.57745/RDMHWY); Fineli = CC BY 4.0 (attribute
Finnish Institute for Health and Welfare (THL), Fineli, release from
`descript.txt`). fineli.fi sits behind a bot-protection challenge — never
try to get around it; `download_fineli.py --zip` takes a browser-saved
package; Frida (Danish Food Composition Database) dataset = CC BY 4.0
(attribute "The Danish Food Composition Database (https://fcdb.fooddata.dk),
version 6.1, May 2026, National Food Institute, Technical University of
Denmark", https://doi.org/10.11583/DTU.32312844; its documentation PDFs are
© DTU, not CC BY). Always state licensing when adding a source.

## Style

Concise, self-documenting scripts: thorough module docstrings (usage,
notes, import commands), standard library where practical, clear `error:`
messages that tell the user what to do next. Conventional Commits for
messages (`feat`, `fix`, `docs`; scope like `feat(bls):`).
