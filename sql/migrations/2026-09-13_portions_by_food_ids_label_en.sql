-- Returns the English portion description beside the coalesced label, so the
-- app can match a model-named portion word against the vocabulary the model
-- was told to answer in.
--
-- `portions_by_food_ids` serves `label` as the reader's verified translation
-- where one exists and the English text otherwise, with `localized` saying
-- which. That is the right shape for display and the wrong shape for matching
-- a word an AI model emitted. The prompt pins `portion` to English in every
-- locale (simonoppowa/OpenNutriTracker#1157, decided 2026-09-11) because
-- English is what the labels are in eight of the nine locales the app ships;
-- the ninth is German, where 11,588 rows carry a verified translation — the
-- one locale whose review has actually been done. An English key matched
-- against `label` therefore fails exactly where the translation effort was
-- spent. Matched against the English text it works in all nine locales
-- regardless of how far the #1093 review has got, and the typed path — whose
-- words are in the reader's language — keeps matching the localized label as
-- it does today. App issue simonoppowa/OpenNutriTracker#1154, hand-off step 1.
--
-- The string is `fp.portion_description`, which the function already reads:
-- it is the fallback inside the coalesce and every WHERE clause tests it. It
-- was simply never selected. `label_en` is never null on a returned row,
-- because the WHERE keeps only rows whose description is present; and
-- `label = label_en` exactly when `localized` is false. Body, filter, order
-- and the five existing columns are unchanged.
--
-- Adding a column to `returns table` changes the function's return type, and
-- `create or replace` refuses that ("cannot change return type of existing
-- function"). So the function is dropped and recreated inside one
-- transaction — no caller can observe the gap — and the revoke/grant is
-- issued again, because the privileges go with the dropped function and a
-- fresh one is executable by PUBLIC. That is the pair
-- 2026-08-30_portions_by_food_ids.sql issued when it created the function,
-- and it leaves the ACL as it is today: postgres, anon, authenticated,
-- service_role. Nothing depends on the function (no view, trigger or
-- SQL-standard function body references it), so the plain DROP succeeds.
--
-- Measured before this was written (the body below run as a plain SELECT
-- against production, read-only, over the first five results each of
-- search_food_summary('bread') and search_food_summary('chicken breast'),
-- ten foods):
--   * loc = 'de': 59 rows, 39 localized, and `label = label_en` on exactly
--     the other 20. Bread, rye (2707755) row 4 is "1 mittlere oder normale
--     Scheibe" beside "1 medium or regular slice", 32.0 g; Chicken breast,
--     rotisserie, skin eaten (2705963) row 2 is "1 breast" beside "1 breast",
--     130.0 g, localized false — the row a German reader still gets in
--     English.
--   * loc = 'en': the same 59 rows, `localized` false and `label = label_en`
--     on every one.
--   * Over every food in food_portion (15,441 deliverable rows, 5,375 foods):
--     `label_en` is null on none, and `label <> label_en` on exactly the
--     11,588 rows where `localized` is true for 'de' and on none for 'en' or
--     'fr' — the only verified locale today is 'de'.
--   * The plan is the old plan: the function inlines into a nested loop from
--     unnest(ids) into idx_food_portion_food and one primary-key probe per
--     portion into food_portion_translation, 191 shared buffers for the ten
--     foods. The column is already fetched; selecting it adds nothing.
--
-- Safe to run before the app change ships: the shipped app reads the RPC's
-- rows by key and ignores one it does not know, so the extra column is
-- invisible to it. The app half (#1154 steps 3–5) is built against this exact
-- shape and reads `label_en`, so deploy this first. PostgREST reloads its
-- schema cache on the DDL (`pgrst_ddl_watch`), so the new column is served on
-- the next call.
--
-- Run with:
--   psql "$SUPABASE_DB_URL" -f sql/migrations/2026-09-13_portions_by_food_ids_label_en.sql

begin;

drop function portions_by_food_ids(bigint[], text);

create function portions_by_food_ids(
    ids  bigint[],
    loc  text
)
returns table (
    food_id      bigint,
    seq          int,
    label        text,
    localized    boolean,
    gram_weight  numeric,
    label_en     text
)
language sql
stable
as $$
    select
        fp.food_id,
        row_number() over (
            partition by fp.food_id
            order by fp.seq_num nulls last, fp.id
        )::int,
        coalesce(t.portion_description, fp.portion_description),
        t.portion_description is not null,
        fp.gram_weight,
        fp.portion_description
    from food_portion fp
    join unnest(ids) as w(id) on w.id = fp.food_id
    left join food_portion_translation t
           on t.food_portion_id = fp.id
          and t.locale = loc
          and t.source = 'verified'
          and t.portion_description is not null
          and btrim(t.portion_description) <> ''
    where fp.portion_description is not null
      and fp.portion_description <> 'Quantity not specified'
      and fp.gram_weight is not null
      and fp.gram_weight > 0
      and fp.portion_description !~* '\mNFS\M|\mNS as to\M|\myields\M|^Guideline amount'
    order by fp.food_id, fp.seq_num nulls last, fp.id;
$$;

revoke execute on function portions_by_food_ids(bigint[], text) from public;
grant execute on function portions_by_food_ids(bigint[], text)
    to anon, authenticated;

commit;
