-- Restores idx_food_summary_name_trgm on databases that had it.
--
-- 2026-09-13_food_summary_has_portion.sql recreates food_summary and then
-- rebuilds the index set production has — five indexes, no trigram one,
-- because pg_trgm is not installed there and the 2026-07-13 line that
-- created it would have aborted the transaction. A database bootstrapped
-- from sql/schema.sql is different: schema.sql installs pg_trgm and
-- declares idx_food_summary_name_trgm, so on such a database the
-- 2026-09-13 migration dropped that index with the view and did not put
-- it back. Fuzzy `ILIKE '%term%'` on food_summary.name lost its GIN
-- acceleration there. Found by review on Backend#11.
--
-- This creates the index only where the extension exists, so it is a
-- no-op on production (no pg_trgm) and idempotent everywhere (IF NOT
-- EXISTS). Safe to run at any time; a GIN build over 20,834 rows takes
-- seconds and does not lock the view for readers.
--
-- Run with:
--   psql "$SUPABASE_DB_URL" -f sql/migrations/2026-09-18_food_summary_trgm_index.sql

do $$
begin
    if exists (select 1 from pg_extension where extname = 'pg_trgm') then
        execute 'create index if not exists idx_food_summary_name_trgm '
                'on food_summary using gin (name gin_trgm_ops)';
    else
        raise notice 'pg_trgm is not installed; idx_food_summary_name_trgm not created';
    end if;
end
$$;
