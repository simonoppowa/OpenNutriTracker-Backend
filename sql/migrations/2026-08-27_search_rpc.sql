-- Adds the three app-facing search functions so a food search stops
-- travelling in a URL.
--
-- PostgREST turns a table query into a GET, so the search term lands in the
-- query string and the Supabase API gateway log keeps it — as request.url
-- and request.search — in the same record as the caller's IP, city, postal
-- code, ISP and TLS fingerprint. An RPC is a POST with a JSON body, and the
-- gateway log has no body field. The IP and geolocation still get recorded;
-- what goes away is their pairing with what the user typed.
-- App issue simonoppowa/OpenNutriTracker#882.
--
-- Safe to run before the app change ships: nothing calls these until the
-- app is updated, and the existing view grants are untouched, so the
-- current build keeps working throughout. Deploy in that order.
--
-- Run with: psql "$SUPABASE_DB_URL" -f sql/migrations/2026-08-27_search_rpc.sql

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
    limit greatest(max_rows, 0)
$$;

create or replace function food_summary_by_ids(
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

revoke execute on function search_food_summary(text, text[], int) from public;
revoke execute on function search_food_translation(text, text, int) from public;
revoke execute on function food_summary_by_ids(bigint[], text[]) from public;
grant execute on function search_food_summary(text, text[], int)
    to anon, authenticated;
grant execute on function search_food_translation(text, text, int)
    to anon, authenticated;
grant execute on function food_summary_by_ids(bigint[], text[])
    to anon, authenticated;

commit;

-- Run this one SEPARATELY and outside the transaction above. food_summary
-- has no full-text index on `name` today, so English search is already a
-- sequential scan — the RPC inherits that, it does not cause it. CONCURRENTLY
-- keeps search answering while the index builds; a plain CREATE INDEX would
-- lock the view for the duration.
--
--   psql "$SUPABASE_DB_URL" -c "create index concurrently if not exists \
--       idx_food_summary_name_fts on food_summary using gin (to_tsvector('english', name));"
