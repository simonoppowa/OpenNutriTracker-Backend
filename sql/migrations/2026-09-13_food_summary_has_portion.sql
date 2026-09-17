-- Stores "has a deliverable portion" on every search row, so the app's cut
-- to twenty can see it.
--
-- Since 2026-09-12_search_rpc_order_by_portions.sql the 100-row pool leads
-- with the records that have a labelled portion, but the rows themselves do
-- not say which ones those are. The app scores the pool on its titles, keeps
-- twenty, and only then fetches portions (`portions_by_food_ids`) — so the
-- two rules that prefer a portion-bearing record live in the resolver, which
-- never sees a record the cut discarded. "muffins" is the exact title of
-- twenty SR Legacy "Muffins, …" rows (no portions) and a near miss for the
-- survey "Muffin" family (five portions): the twenty are kept, "Muffin, NFS"
-- is 21st and gone, and the resolver settles on "Muffins, oat bran" with
-- nothing to scale "2 muffins" by. Same shape for puddings and ice creams.
-- App issue simonoppowa/OpenNutriTracker#1190.
--
-- Needs 2026-09-12_search_rpc_order_by_portions.sql (the helper) applied
-- first; the order keys from 2026-09-13_search_rpc_order_title_first.sql are
-- carried forward here, so this runs correctly with or without that one.
--
-- What changes:
--   * food_summary gains `has_portion boolean`, computed at refresh as
--     food_has_deliverable_portion(food_id) — the predicate
--     portions_by_food_ids filters by, so the flag means exactly "the picker
--     would have something to show". A materialized view cannot declare
--     NOT NULL, but EXISTS never yields NULL, so the column never is.
--     food_portion changes only through the import pipeline, which ends with
--     a refresh, so the flag cannot be staler than serving_size.
--   * search_food_summary and food_summary_by_ids keep `returns setof
--     food_summary`; the column rides along. search_food_summary orders by
--     the stored column instead of calling the helper per match.
--   * search_food_translation returns one more column, `has_portion`, taken
--     from food_summary by a left join (coalesced to false; every
--     food_translation row has a summary row today, and the FK to food keeps
--     it so). A return-type change, so the function is dropped and recreated
--     and its grants re-issued. The helper is now only called at refresh.
--
-- Measured read-only against production on 2026-09-13 (warm cache):
--   * The join is the cheaper way to get the flag in the translation search,
--     median of five: Kartoffel 0.69 ms vs 2.61 ms with the helper per row,
--     Milch (307 matches) 1.82 vs 6.19, Brot 1.13 vs 4.49, Ei 0.95 vs 3.51.
--     It also reads the same stored flag the app receives from
--     food_summary_by_ids on its second hop, so the two cannot disagree.
--   * search_food_summary('potato') (712 matches): 15.9 ms with the helper in
--     ORDER BY, 4.5 ms ordering by a stored column.
--   * Pools are unchanged — the order keys are the same values, stored
--     instead of computed. potato: 712 matches, 327 with a portion, all 100
--     pool rows flagged, first "Potato, NFS". muffins: 79 matches, 40 with a
--     portion, first "Muffin, NFS". orange juice: 45 matches, 9 with a
--     portion, first "Orange juice, 100%, NFS". Kartoffel (de): 108 matches,
--     62 with a portion, first "Kartoffel, NFS".
--   * The view's SELECT: 20,834 rows; 17 s with today's definition, 44–49 s
--     with the new column. The helper has SET search_path, which stops the
--     planner inlining it, so it runs as a separate executor call per food;
--     inlining the EXISTS by hand brings the SELECT back to ~14 s, but then
--     the predicate lives in two places, which the 2026-09-12 header ruled
--     out. Every later `refresh materialized view food_summary` (end of each
--     import) pays the same ~30 s.
--
-- What is unavailable while this runs — plan for about a minute:
--   Adding a column to a materialized view means dropping and recreating
--   it, and the functions that return its row type go with it. The whole
--   run is one transaction, so nothing ever sees the view missing, but from
--   the DROP until COMMIT the view is exclusively locked: every
--   search_food_summary and food_summary_by_ids call blocks on that lock,
--   and PostgREST's roles have statement_timeout 3 s (anon) / 8 s
--   (authenticated), so English search and the second hop of a localized
--   search fail for the duration (the populate measured 44–49 s, plus five
--   index builds over 20,834 rows). search_food_translation itself keeps
--   answering on its old definition until COMMIT. After COMMIT the next call
--   works: the names and parameters are unchanged, and PostgREST reloads its
--   schema cache on DDL (pgrst_ddl_watch / pgrst_drop_watch). Run it at a
--   quiet hour. If it fails at any point, nothing has changed.
--
-- Not taken: keeping the view and adding the flag to the RPCs' select lists
-- instead. search_food_summary and food_summary_by_ids would have to spell
-- out the view's 39 columns in a `returns table` (three copies of the column
-- list to keep in step), the flag would be computed on every search rather
-- than once per refresh (the 15.9 vs 4.5 ms above), and the second hop
-- would carry it only if food_summary_by_ids changed too. One column on the
-- view gives every consumer the flag from one definition, at the cost of
-- this one recreation and a slower refresh.
--
-- The index set below is what production has. The trgm index the 2026-07-13
-- migration created is not among them: pg_trgm is not installed, so that
-- CREATE INDEX would abort the transaction. idx_food_summary_name_fts (from
-- 2026-08-27) is built here inside the transaction: the view is invisible
-- until COMMIT anyway, so there is nothing for CONCURRENTLY to keep
-- answering.
--
-- Run with:
--   psql "$SUPABASE_DB_URL" -f sql/migrations/2026-09-13_food_summary_has_portion.sql

begin;

-- The functions that return the view's row type, dropped by name rather
-- than by CASCADE so that any other dependent that has appeared since aborts
-- the run instead of vanishing with it. search_food_translation does not
-- depend on the view; it goes because its return type changes.
drop function search_food_summary(text, text[], int);
drop function food_summary_by_ids(bigint[], text[]);
drop function search_food_translation(text, text, int);

drop materialized view food_summary;

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
    n.niacin_100,
    -- True when portions_by_food_ids would return at least one row for this
    -- food — the same helper the search order has used since 2026-09-12, now
    -- stored so the app's cut can read it (#1190). Never NULL: it is an
    -- EXISTS. Recomputed on every refresh.
    food_has_deliverable_portion(f.id)     as has_portion
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
create index idx_food_summary_tags
    on food_summary using gin (tags);   -- WHERE tags @> array['vegan']
create index idx_food_summary_barcode   -- barcode scan lookup (branded/OFF)
    on food_summary (barcode) where barcode is not null;
create index idx_food_summary_name_fts  -- what search_food_summary runs on
    on food_summary using gin (to_tsvector('english', name));

-- Materialized views have no RLS; restrict via grants instead. The live view
-- is readable by service_role as well (pg_class.relacl, read 2026-09-17),
-- which the 2026-07-13 precedent left to pg_default_acl; named here so the
-- ACL after this run does not rest on that setting.
revoke all on food_summary from anon, authenticated, service_role;
grant select on food_summary to anon, authenticated, service_role;

create function search_food_summary(
    term      text,
    sources   text[] default null,
    max_rows  int    default 100
)
returns setof food_summary
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
    select fs.*
    from food_summary fs
    where to_tsvector('english', fs.name)
          @@ websearch_to_tsquery('english', term)
      and (sources is null or fs.source = any (sources))
    order by fs.has_portion desc,
             (lower(btrim(split_part(fs.name, ',', 1))) = lower(btrim(term))) desc,
             length(fs.name),
             fs.food_id
    limit greatest(max_rows, 0)
$$;

create function search_food_translation(
    term      text,
    loc       text,
    max_rows  int default 100
)
returns table (food_id bigint, description text, source text, has_portion boolean)
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
    select ft.food_id, ft.description, ft.source,
           coalesce(fs.has_portion, false) as has_portion
    from food_translation ft
    left join food_summary fs on fs.food_id = ft.food_id
    where ft.locale = loc
      and to_tsvector('simple', ft.description)
          @@ websearch_to_tsquery('simple', term)
    order by coalesce(fs.has_portion, false) desc,
             (lower(btrim(split_part(ft.description, ',', 1))) = lower(btrim(term))) desc,
             length(ft.description),
             ft.food_id
    limit greatest(max_rows, 0)
$$;

create function food_summary_by_ids(
    ids      bigint[],
    sources  text[] default null
)
returns setof food_summary
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
    select fs.*
    from food_summary fs
    where fs.food_id = any (ids)
      and (sources is null or fs.source = any (sources))
$$;

-- Recreated functions come back executable by PUBLIC; the grant is only
-- meaningful after the revoke. service_role is named because the live
-- functions are executable by it today (pg_proc.proacl) only through
-- pg_default_acl, and a drop discards the object ACL — same reasoning as
-- 2026-09-13_portions_by_food_ids_label_en.sql.
revoke execute on function search_food_summary(text, text[], int) from public;
revoke execute on function search_food_translation(text, text, int) from public;
revoke execute on function food_summary_by_ids(bigint[], text[]) from public;
grant execute on function search_food_summary(text, text[], int)
    to anon, authenticated, service_role;
grant execute on function search_food_translation(text, text, int)
    to anon, authenticated, service_role;
grant execute on function food_summary_by_ids(bigint[], text[])
    to anon, authenticated, service_role;

commit;

-- Spot-check (expect survey rows flagged, BLS and SR Legacy not):
--   select source, count(*) filter (where has_portion) as with_portion, count(*)
--   from food_summary group by source order by 1;
--   -- fdc_survey 5374 of 5432, fdc_foundation 1 of 469, bls and fdc_sr_legacy 0
--   select food_id, has_portion, name from search_food_summary('muffins') limit 3;
--   -- Muffin, NFS · Muffin, fruit · Muffin, wheat, all true
