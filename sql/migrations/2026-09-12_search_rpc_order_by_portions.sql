-- Orders both search RPCs so the pool the app receives no longer depends on
-- heap order.
--
-- `search_food_summary` and `search_food_translation` return the first
-- `max_rows` matches in whatever order the heap yields them. In
-- `food_translation` that order puts SR Legacy and BLS rows first, so the
-- 100-row German pool for "Milch" is 100 BLS rows, for "Reis" 56 SR Legacy
-- and 44 BLS, for "Ei" one survey record in a hundred — and neither BLS nor
-- SR Legacy carries a labelled portion (BLS has no portion rows at all; SR
-- Legacy's have a NULL description). The survey records, which all have
-- German names and are the only ones with portions, never reach the app, so
-- the portion picker and every verified German portion label are unreachable
-- from a German search. App issues simonoppowa/OpenNutriTracker#1165 and
-- #1164.
--
-- The order is "records the app would receive at least one portion for,
-- first; then food_id". The portion test is `portions_by_food_ids`'s own
-- deliverable filter, so the flag means exactly "the picker would have
-- something to show". It is deliberately not a source preference: a BLS
-- record that gains portions moves up on its own, and a survey record that
-- has none does not ride on its source.
--
-- Measured before this was written (100-row pool, records with a labelled
-- portion): Milch 0 → 85, Reis 0 → 100, Ei 1 → 57, Apfel 0 → 8, Brot
-- 13 → 100. English pools already lead with survey rows and keep their
-- content; they become deterministic.
--
-- The app's own ranking then reorders whatever the pool contains, so this
-- changes what is *available* to rank, not what wins a tie — that half is
-- app-side (#1164).
--
-- Safe to run at any time: both functions keep their signatures and return
-- types, and the app already calls them.
--
-- Run with:
--   psql "$SUPABASE_DB_URL" -f sql/migrations/2026-09-12_search_rpc_order_by_portions.sql

begin;

-- True when `portions_by_food_ids` would return at least one row for the
-- food: the same predicate, kept in one place so the two cannot drift.
create or replace function food_has_deliverable_portion(fid bigint)
returns boolean
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
    select exists (
        select 1
        from food_portion fp
        where fp.food_id = fid
          and fp.portion_description is not null
          and fp.portion_description <> 'Quantity not specified'
          and fp.gram_weight is not null
          and fp.gram_weight > 0
          and fp.portion_description !~* '\mNFS\M|\mNS as to\M|\myields\M|^Guideline amount'
    )
$$;

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
    order by food_has_deliverable_portion(fs.food_id) desc, fs.food_id
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
    order by food_has_deliverable_portion(ft.food_id) desc, ft.food_id
    limit greatest(max_rows, 0)
$$;

-- The helper is only ever called from inside the two search functions; it is
-- not an API of its own.
revoke execute on function food_has_deliverable_portion(bigint) from public;
grant execute on function food_has_deliverable_portion(bigint)
    to anon, authenticated;

commit;
