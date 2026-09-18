-- Second key for the search order: the records whose title is the term,
-- ahead of the records that merely mention it.
--
-- 2026-09-12_search_rpc_order_by_portions.sql put records with a labelled
-- portion first and then fell back to food_id. Within the portion-bearing
-- set that fills the 100-row pool by id, which for a large family means by
-- whatever else contains the word: search_food_summary('potato') matches
-- 712 rows, and the 100 the app receives are "Beef and potatoes, no sauce"
-- and its neighbours — not one row titled "Potato". "Potato, NFS" is rank
-- 128. The same for bread (rye at 157), rice (147), chicken (154), carrots
-- (199): the generic record of the family the user named never reaches the
-- app, so nothing the app ranks can choose it.
-- App issues simonoppowa/OpenNutriTracker#1164 and #1165.
--
-- The order is now: has a labelled portion; then the title — the text before
-- the first comma, which is FDC's own short title on every row — equals the
-- term; then the shortest description; then food_id. Measured over the 39
-- survey families with more than twenty members: the family's generic record
-- is rank 1 in 36 and rank 2 in the other three, and the family is inside
-- the pool every time. The full-text WHERE already keeps only rows that
-- contain every query token, so a qualified query ("egg yolk") still reaches
-- its qualified record — the title key only decides who leads among matches.
--
-- The translation function compares the translated description's head to
-- the term, so a German query meets a German title.
--
-- Safe to run at any time: signatures and return types unchanged.
--
-- Run with:
--   psql "$SUPABASE_DB_URL" -f sql/migrations/2026-09-13_search_rpc_order_title_first.sql

begin;

create or replace function search_food_summary(
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
    order by food_has_deliverable_portion(fs.food_id) desc,
             (lower(btrim(split_part(fs.name, ',', 1))) = lower(btrim(term))) desc,
             length(fs.name),
             fs.food_id
    limit greatest(max_rows, 0)
$$;

create or replace function search_food_translation(
    term      text,
    loc       text,
    max_rows  int default 100
)
returns table (food_id bigint, description text, source text)
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
    select ft.food_id, ft.description, ft.source
    from food_translation ft
    where ft.locale = loc
      and to_tsvector('simple', ft.description)
          @@ websearch_to_tsquery('simple', term)
    order by food_has_deliverable_portion(ft.food_id) desc,
             (lower(btrim(split_part(ft.description, ',', 1))) = lower(btrim(term))) desc,
             length(ft.description),
             ft.food_id
    limit greatest(max_rows, 0)
$$;

commit;
