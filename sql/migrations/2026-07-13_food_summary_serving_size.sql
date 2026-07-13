-- Recreates food_summary to pick up the serving_size/serving_unit changes
-- (schema.sql as of 2026-07-13):
--   * serving_unit: FDC's 'undetermined' measure unit presented as 'portion'
--   * serving_size: falls back to "<amount> <modifier>" so SR Legacy /
--     Foundation foods expose their household measure ("1 slice") instead
--     of nothing — FDC keeps it in food_portion.modifier for those.
--
-- Needed because schema.sql uses CREATE MATERIALIZED VIEW IF NOT EXISTS,
-- which never updates an existing view. Runs in one transaction so the
-- app's food search never sees the view missing.
--
-- Run with: psql "$SUPABASE_DB_URL" -f sql/migrations/2026-07-13_food_summary_serving_size.sql

begin;

drop materialized view if exists food_summary;

create materialized view food_summary as
select
    f.id                                   as food_id,
    f.source,
    f.source_code,
    f.description                          as name,
    coalesce(f.short_title, f.description) as short_title,  -- concise label
    ma.brand_description                   as brands,      -- MealDBO.brands
    ma.upc_code                            as barcode,     -- scanned lookup
    fc.description                         as category,
    p.amount                               as serving_quantity,
    -- FDC's measure_unit 9999 is literally named 'undetermined' (a portion
    -- with no household measure). Present it as 'portion' — the label the
    -- app used to map id 9999 to — instead of leaking the raw name.
    case when mu.name = 'undetermined' then 'portion' else mu.name end
                                           as serving_unit,
    -- Household measure of the default portion. FDC splits it across two
    -- columns: FNDDS writes prose into portion_description, while SR
    -- Legacy/Foundation leave that empty and put the measure text ("slice",
    -- "cup, sliced") into modifier — their measure_unit is 'undetermined'
    -- precisely because the real unit lives there. Fall back to
    -- "<amount> <modifier>" so those foods get "1 slice" instead of nothing.
    coalesce(
        nullif(p.portion_description, ''),
        case when nullif(p.modifier, '') is not null
             then trim(concat_ws(' ', p.amount::text, p.modifier))
        end
    )                                      as serving_size,
    p.gram_weight                          as serving_gram_weight,
    coalesce(it.external_url, '/storage/v1/object/public/food-images/' || it.storage_path)
                                           as thumbnail_url,
    coalesce(im.external_url, '/storage/v1/object/public/food-images/' || im.storage_path)
                                           as main_image_url,
    tg.tags,
    n.energy_kcal_100,
    n.carbohydrates_100,
    n.fat_100,
    n.proteins_100,
    n.sugars_100,
    n.saturated_fat_100,
    n.fiber_100,
    n.monounsaturated_fat_100,
    n.polyunsaturated_fat_100,
    n.trans_fat_100,
    n.cholesterol_100,
    n.sodium_100,
    n.potassium_100,
    n.magnesium_100,
    n.calcium_100,
    n.iron_100,
    n.zinc_100,
    n.phosphorus_100,
    n.vitamin_a_100,
    n.vitamin_c_100,
    n.vitamin_d_100,
    n.vitamin_b6_100,
    n.vitamin_b12_100,
    n.niacin_100
from food f
left join food_category fc on fc.id = f.food_category_id
left join market_acquisition ma on ma.food_id = f.id   -- brand + barcode
left join lateral (                      -- first portion = default serving
    select fp.amount, fp.portion_description, fp.modifier, fp.gram_weight,
           fp.measure_unit_id
    from food_portion fp
    where fp.food_id = f.id
    order by fp.seq_num nulls last, fp.id
    limit 1
) p on true
left join measure_unit mu on mu.id = p.measure_unit_id
left join lateral (
    select fi.external_url, fi.storage_path from food_image fi
    where fi.food_id = f.id and fi.kind = 'thumbnail'
    order by fi.id limit 1
) it on true
left join lateral (
    select fi.external_url, fi.storage_path from food_image fi
    where fi.food_id = f.id and fi.kind = 'main'
    order by fi.id limit 1
) im on true
left join lateral (
    select array_agg(t.slug order by t.slug) as tags
    from food_tag ft2
    join tag t on t.id = ft2.tag_id
    where ft2.food_id = f.id
) tg on true
left join lateral (
    select
        max(fn.amount) filter (where fn.nutrient_id = 1)  as energy_kcal_100,
        max(fn.amount) filter (where fn.nutrient_id = 2)  as carbohydrates_100,
        max(fn.amount) filter (where fn.nutrient_id = 3)  as fat_100,
        max(fn.amount) filter (where fn.nutrient_id = 4)  as proteins_100,
        max(fn.amount) filter (where fn.nutrient_id = 5)  as sugars_100,
        max(fn.amount) filter (where fn.nutrient_id = 6)  as saturated_fat_100,
        max(fn.amount) filter (where fn.nutrient_id = 7)  as fiber_100,
        max(fn.amount) filter (where fn.nutrient_id = 8)  as monounsaturated_fat_100,
        max(fn.amount) filter (where fn.nutrient_id = 9)  as polyunsaturated_fat_100,
        max(fn.amount) filter (where fn.nutrient_id = 10) as trans_fat_100,
        max(fn.amount) filter (where fn.nutrient_id = 11) as cholesterol_100,
        max(fn.amount) filter (where fn.nutrient_id = 12) as sodium_100,
        max(fn.amount) filter (where fn.nutrient_id = 13) as potassium_100,
        max(fn.amount) filter (where fn.nutrient_id = 14) as magnesium_100,
        max(fn.amount) filter (where fn.nutrient_id = 15) as calcium_100,
        max(fn.amount) filter (where fn.nutrient_id = 16) as iron_100,
        max(fn.amount) filter (where fn.nutrient_id = 17) as zinc_100,
        max(fn.amount) filter (where fn.nutrient_id = 18) as phosphorus_100,
        max(fn.amount) filter (where fn.nutrient_id = 19) as vitamin_a_100,
        max(fn.amount) filter (where fn.nutrient_id = 20) as vitamin_c_100,
        max(fn.amount) filter (where fn.nutrient_id = 21) as vitamin_d_100,
        max(fn.amount) filter (where fn.nutrient_id = 22) as vitamin_b6_100,
        max(fn.amount) filter (where fn.nutrient_id = 23) as vitamin_b12_100,
        max(fn.amount) filter (where fn.nutrient_id = 24) as niacin_100
    from food_nutrient fn
    where fn.food_id = f.id
) n on true;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY:
create unique index idx_food_summary_id on food_summary (food_id);
create index idx_food_summary_source on food_summary (source);
create index idx_food_summary_name_trgm
    on food_summary using gin (name gin_trgm_ops);
create index idx_food_summary_tags
    on food_summary using gin (tags);   -- WHERE tags @> array['vegan']
create index idx_food_summary_barcode   -- barcode scan lookup (branded/OFF)
    on food_summary (barcode) where barcode is not null;

-- Materialized views have no RLS; restrict via grants instead.
revoke all on food_summary from anon, authenticated;
grant select on food_summary to anon, authenticated;

commit;

-- Spot-check (SR Legacy example: expect household measures, not NULL):
--   select name, serving_quantity, serving_unit, serving_size
--   from food_summary where source = 'fdc_sr_legacy'
--     and serving_size is not null limit 10;
