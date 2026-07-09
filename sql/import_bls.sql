-- ============================================================
-- Bulk import of the generated BLS 4.0 CSVs into Supabase.
-- Appends to the existing FDC data - run AFTER import_fdc.sql
-- (the shared canonical `nutrient` table ships with the FDC CSVs).
--
-- Run from the folder containing the BLS CSVs
-- (output of bls_to_ont_csv.py):
--   psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f import_bls.sql
--
-- \copy runs client-side: use psql, not the dashboard SQL Editor.
-- ============================================================

\timing on

-- ---- 0. Remove any previous BLS import (idempotent re-runs) --
-- Cascades wipe BLS rows in food_nutrient / food_translation etc.
-- FDC rows are untouched.
begin;
delete from food             where source = 'bls';
delete from nutrient_mapping where source = 'bls';
delete from bls_component;
delete from food_source      where code = 'bls';
commit;

-- ---- 1. Load in FK-safe order --------------------------------
begin;

\copy food_source from 'food_source.csv' with (format csv, header true)
\copy bls_component from 'bls_component.csv' with (format csv, header true)
\copy nutrient_mapping from 'nutrient_mapping.csv' with (format csv, header true)
\copy food from 'food.csv' with (format csv, header true)
\copy food_translation (food_id, locale, description, source) from 'food_translation.csv' with (format csv, header true)
\copy food_nutrient from 'food_nutrient.csv' with (format csv, header true)

commit;

-- ---- 2. Rebuild the app-facing view & stats -------------------
refresh materialized view food_summary;
analyze;

-- ---- 3. Sanity checks ------------------------------------------
-- Expected: 7,140 bls foods, all with kcal, German names for all.
select source, count(*) as foods
from food_summary
group by source
order by source;

select count(*) as bls_foods_with_german_name
from food f
join food_translation t on t.food_id = f.id and t.locale = 'de'
where f.source = 'bls';

select fs.source_code,
       fs.name,
       ft.description as name_de,
       fs.energy_kcal_100,
       fs.proteins_100
from food_summary fs
left join food_translation ft
       on ft.food_id = fs.food_id and ft.locale = 'de'
where fs.source = 'bls'
  and fs.name ilike '%oat flakes%'
limit 3;
