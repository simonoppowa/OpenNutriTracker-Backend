-- Serves every usable portion a food has, so the app can offer a choice
-- instead of the single one `food_summary` happens to pick.
--
-- `portion_labels_by_food_ids` answers "what is this food's default portion
-- called in my language". This answers "what portions does it have at all",
-- which is what a picker needs. 3,499 foods carry two or more usable
-- portions and 2,239 carry three or more, so the single default is a real
-- loss rather than a theoretical one — a food measured in cups, slices and
-- ounces currently offers whichever of those sorted first.
-- App issue simonoppowa/OpenNutriTracker#864.
--
-- **Usable** excludes what a person cannot read or count:
--   * no description, or FDC's "Quantity not specified"
--   * FDC survey bookkeeping — NFS, "NS as to size", "yields",
--     "Guideline amount ..." — which is poor UI in English too
--   * a gram weight of zero or null, which cannot scale an amount
--
-- Each row carries both the English text and the reader's, plus which one
-- `label` holds, because the app has to know: "1 Scheibe" and "1 slice" are
-- indistinguishable as strings, and showing the English one to a German
-- reader is the defect #966 gated against. `localized` is false unless a
-- human promoted that locale to 'verified'.
--
-- Ordered by `seq_num` like `food_summary`'s lateral pick, so the first row
-- here is the portion the app already treats as the default and a picker
-- opens on the same choice it makes today.
--
-- A function rather than a table read for the reason #882 gave: a GET puts
-- the food ids in the gateway log beside the caller's IP.
--
-- Safe to run before the app change ships: nothing calls it until then.
--
-- Run with:
--   psql "$SUPABASE_DB_URL" -f sql/migrations/2026-08-30_portions_by_food_ids.sql

begin;

create or replace function portions_by_food_ids(
    ids  bigint[],
    loc  text
)
returns table (
    food_id      bigint,
    seq          int,
    label        text,
    localized    boolean,
    gram_weight  numeric
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
        fp.gram_weight
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
