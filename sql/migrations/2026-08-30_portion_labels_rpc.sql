-- Serves the app a food's default portion label in the user's language, and
-- only when a human has verified it.
--
-- `food_summary.serving_size` is built from `food_portion.portion_description`
-- with no translation join, so it is English for every locale the app ships.
-- The translations now exist (2026-08-30_food_portion_translation_seed.sql)
-- but nothing can reach them: `food_summary` is a materialized view with a
-- fixed row type, and `food_summary_by_ids` returns `setof food_summary`, so
-- neither can carry a locale-dependent column.
-- App issue simonoppowa/OpenNutriTracker#864.
--
-- **A function rather than a table read, for the reason #882 gave.** PostgREST
-- turns a table query into a GET, so `food_id=eq.12345` lands in the API
-- gateway log next to the caller's IP, city and TLS fingerprint — a record of
-- which foods that person opened. An RPC is a POST with a JSON body and the
-- gateway log has no body field.
--
-- **The 'verified' filter lives here, not in the app.** Seeded rows are
-- machine output and are not fit to show; enforcing that server-side means a
-- client cannot display them by forgetting to ask, and a locale becomes
-- visible the moment a reviewer promotes it, with no app release.
--
-- The lateral pick is copied from `food_summary` on purpose: the label has to
-- describe the same portion whose `gram_weight` became `serving_gram_weight`,
-- or the word and the number disagree — "1 slice" beside 240 g because the
-- label came from one row and the weight from another.
--
-- Returns nothing for a food with no verified translation, which the caller
-- reads as "keep showing what you show today".
--
-- Safe to run before the app change ships: nothing calls it until the app is
-- updated. Deploy in that order.
--
-- Run with:
--   psql "$SUPABASE_DB_URL" -f sql/migrations/2026-08-30_portion_labels_rpc.sql

begin;

create or replace function portion_labels_by_food_ids(
    ids  bigint[],
    loc  text
)
returns table (food_id bigint, label text)
language sql
stable
as $$
    select f.id, t.portion_description
    from unnest(ids) as f(id)
    join lateral (
        select fp.id
        from food_portion fp
        where fp.food_id = f.id
        order by fp.seq_num nulls last, fp.id
        limit 1
    ) p on true
    join food_portion_translation t
      on t.food_portion_id = p.id
     and t.locale = loc
     and t.source = 'verified'
    where t.portion_description is not null
      and btrim(t.portion_description) <> '';
$$;

revoke execute on function portion_labels_by_food_ids(bigint[], text) from public;
grant execute on function portion_labels_by_food_ids(bigint[], text)
    to anon, authenticated;

commit;
