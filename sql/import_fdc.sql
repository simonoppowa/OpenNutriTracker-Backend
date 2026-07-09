-- ============================================================
-- Bulk import of the generated FDC CSVs into Supabase.
--
-- Run from the folder containing the CSVs:
--   psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f import_fdc.sql
--
-- Notes:
-- * \copy runs CLIENT-side (psql reads your local files), so this
--   script works against hosted Supabase. Plain COPY would not:
--   the server cannot see your filesystem. For the same reason
--   this script CANNOT run in the dashboard's SQL Editor - use
--   psql (or the session pooler connection string, port 5432).
-- * Paths are relative to the directory psql is started from.
-- * Empty CSV fields are imported as NULL (CSV default).
-- * market_acquisition.csv / food_component.csv are header-only
--   for Foundation + SR Legacy; \copy handles them fine (0 rows).
-- ============================================================

\timing on

-- ---- 0. Clean slate (idempotent re-imports) -----------------
begin;
truncate
    food_source,
    nutrient,
    nutrient_mapping,
    food_category,
    food,
    food_nutrient,
    measure_unit,
    food_portion,
    market_acquisition,
    input_food,
    food_component,
    retention_factor,
    food_image
    restart identity cascade;
commit;

-- ---- 1. Load in FK-safe order (parents before children) -----
begin;

\copy food_source        from 'food_source.csv'        with (format csv, header true)
\copy nutrient           from 'nutrient.csv'           with (format csv, header true)
\copy nutrient_mapping   from 'nutrient_mapping.csv'   with (format csv, header true)
\copy food_category      from 'food_category.csv'      with (format csv, header true)
\copy food               from 'food.csv'               with (format csv, header true)
\copy food_nutrient      from 'food_nutrient.csv'      with (format csv, header true)
\copy measure_unit       from 'measure_unit.csv'       with (format csv, header true)
\copy food_portion       from 'food_portion.csv'       with (format csv, header true)
\copy market_acquisition from 'market_acquisition.csv' with (format csv, header true)
\copy input_food         from 'input_food.csv'         with (format csv, header true)
\copy food_component     from 'food_component.csv'     with (format csv, header true)
\copy retention_factor   from 'retention_factor.csv'   with (format csv, header true)
\copy food_image (id, food_id, kind, storage_path, external_url, width, height, attribution, ai_generated) from 'food_image.csv' with (format csv, header true)

commit;

-- food_image.id is an identity column; realign its sequence after
-- importing explicit ids so future inserts don't collide:
select setval(pg_get_serial_sequence('food_image', 'id'),
              coalesce(max(id), 1)) from food_image;

-- ---- 2. Rebuild the app-facing view & stats ------------------
refresh materialized view food_summary;
analyze;

-- ---- 3. Sanity checks ----------------------------------------
select 'foods' as tbl, count(*) from food
union all select 'food_nutrient', count(*) from food_nutrient
union all select 'food_portion', count(*) from food_portion
union all select 'food_summary', count(*) from food_summary
union all select 'foods with kcal', count(*) from food_summary
          where energy_kcal_100 is not null;
