#!/usr/bin/env python3
"""Fixture tests for build_sqlite.py: build a database from a handful of CSV
rows (testdata/sqlite) and check every RPC against what the Postgres
functions return for the same rows.

Run directly, no database needed:

    python3 scripts/shared/test_build_sqlite.py

The expectations were checked against the real functions: the same CSVs
(usda/nutrient.csv is there for Postgres's foreign key only) and the same
review loaded into Postgres 17 with schema.sql, the seed migrations and
CSV COPY, then each RPC called. They agree except where a comment says so.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_sqlite as b  # noqa: E402

DATA = Path(__file__).resolve().parent / "testdata" / "sqlite"


def ids(rows):
    return [r["food_id"] for r in rows]


class FixtureBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name) / "foods.sqlite"
        cls.meta = b.build([DATA / "usda", DATA / "nordic"], out,
                           b.DEFAULT_MIGRATIONS,
                           reviews=[DATA / "review" / "portions_de.csv"],
                           page_size=4096)
        cls.conn = sqlite3.connect(out)
        cls.rpc = b.OfflineRpc(cls.conn)
        cls.out = out

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    # -- files and meta -----------------------------------------------------

    def test_outputs(self):
        self.assertTrue(self.out.with_name("foods.sqlite.gz").exists())
        text = self.out.with_name("ATTRIBUTION.md").read_text()
        self.assertIn("Finnish Institute for Health and Welfare", text)
        self.assertIn("CC0", text)

    def test_meta(self):
        meta = dict(self.conn.execute("select key, value from meta"))
        self.assertEqual(meta["schema_version"], str(b.SCHEMA_VERSION))
        self.assertEqual(meta["food_count"], "12")
        self.assertEqual(json.loads(meta["sources"]),
                         {"fdc_survey": 6, "fdc_sr_legacy": 3, "fineli": 3})
        self.assertEqual(self.conn.execute("pragma freelist_count")
                         .fetchone()[0], 0)
        self.assertEqual(self.conn.execute("select count(*) from food_source")
                         .fetchone()[0], 3)

    # -- food_summary derivations -------------------------------------------

    def summary(self, food_id):
        return self.rpc.food_summary_by_ids([food_id])[0]

    def test_summary_columns_in_view_order(self):
        self.assertEqual(list(self.summary(1001)), b.SUMMARY_COLUMNS)

    def test_default_serving(self):
        row = self.summary(1001)
        self.assertEqual(row["serving_size"], "1 cup")
        self.assertEqual(row["serving_unit"], "portion")   # 'undetermined'
        self.assertIsNone(row["serving_quantity"])
        self.assertEqual(row["serving_gram_weight"], 150.0)
        self.assertIs(row["has_portion"], True)
        # SR Legacy: no description, the measure in modifier.
        row = self.summary(1005)
        self.assertEqual(row["serving_size"], "1.0 medium")
        self.assertIs(row["has_portion"], False)
        # A real unit and nothing to describe it.
        row = self.summary(1008)
        self.assertEqual(row["serving_unit"], "tablespoon")
        self.assertEqual(row["serving_quantity"], 2.0)
        self.assertIsNone(row["serving_size"])
        self.assertIsNone(row["category"])
        # seq_num nulls last: "1 cup" (seq 2) before "1 large" (no seq).
        self.assertEqual(self.summary(1007)["serving_size"], "1 cup")
        # Only NFS / "Quantity not specified" portions: no deliverable one.
        self.assertIs(self.summary(1003)["has_portion"], False)
        self.assertIs(self.summary(1006)["has_portion"], False)
        self.assertIsNone(self.summary(1006)["serving_size"])

    def test_numbers_keep_their_json_type(self):
        row = self.summary(1001)
        self.assertIs(type(row["energy_kcal_100"]), int)         # 77
        self.assertIs(type(row["proteins_100"]), float)          # 2.05
        self.assertIs(type(self.summary(1002)["energy_kcal_100"]), float)
        self.assertIsNone(row["sugars_100"])                     # NULL, not 0
        self.assertEqual(self.summary(1008)["magnesium_100"], 71.1)

    def test_short_title_null_vs_empty(self):
        self.assertEqual(self.summary(1003)["short_title"],
                         "Potatoes, mashed, NS as to form")
        self.assertEqual(self.summary(1004)["short_title"], "")   # quoted ""

    # -- search_food_summary ------------------------------------------------

    def test_search_order(self):
        # has_portion desc, title = term desc, length(name), food_id.
        self.assertEqual(ids(self.rpc.search_food_summary("potato")),
                         [1001, 1002, 1007, 1004, 1006, 1005, 1003])
        self.assertEqual(ids(self.rpc.search_food_summary("  Potato ")),
                         [1001, 1002, 1007, 1004, 1006, 1005, 1003])

    def test_search_cap_and_sources(self):
        self.assertEqual(ids(self.rpc.search_food_summary("potato", None, 2)),
                         [1001, 1002])
        self.assertEqual(ids(self.rpc.search_food_summary("potato", None, -1)),
                         [])
        self.assertEqual(
            ids(self.rpc.search_food_summary("potato", ["fdc_sr_legacy"])),
            [1006, 1005])
        self.assertEqual(ids(self.rpc.search_food_summary("apple")),
                         [1009, 50000002, 50000003])
        self.assertEqual(ids(self.rpc.search_food_summary("apple", ["fineli"])),
                         [50000002, 50000003])
        self.assertEqual(self.rpc.search_food_summary("apple", []), [])

    def test_websearch_syntax(self):
        self.assertEqual(ids(self.rpc.search_food_summary("apple -juice")),
                         [1009, 50000002])
        self.assertEqual(ids(self.rpc.search_food_summary("hummus or juice")),
                         [50000003, 1008])
        self.assertEqual(ids(self.rpc.search_food_summary('"rye bread"')),
                         [50000001])
        self.assertEqual(ids(self.rpc.search_food_summary("bread rye")),
                         [50000001])
        self.assertEqual(self.rpc.search_food_summary('"bread rye"'), [])
        self.assertEqual(self.rpc.search_food_summary("the"), [])  # stopword
        self.assertEqual(ids(self.rpc.search_food_summary("the apple")),
                         [1009, 50000002, 50000003])

    def test_websearch_to_fts5(self):
        f = b.websearch_to_fts5
        self.assertEqual(f("apple", True), '(("apple"))')
        self.assertEqual(f('a "b c" -d or e', True),
                         '(("b c") NOT "d") OR (("e"))')
        self.assertEqual(f('apple -pie or "with skin"', True),
                         '(("apple") NOT "pie") OR (("skin"))')
        self.assertEqual(f("low-fat milk", True), '(("low fat" AND "milk"))')
        self.assertEqual(f("mango/papaya", False), '(("mango/papaya"))')
        self.assertIsNone(f("-cheese", True))
        self.assertIsNone(f("the of", True))
        self.assertEqual(f("the of", False), '(("the" AND "of"))')
        self.assertIsNone(f(None, True))

    # -- search_food_translation --------------------------------------------

    def test_translation_search(self):
        rows = self.rpc.search_food_translation("kartoffel", "de")
        self.assertEqual(rows, [
            {"food_id": 1001, "description": "Kartoffel, NFS",
             "source": "machine", "has_portion": True},
            {"food_id": 1006, "description": "Kartoffel", "source": "machine",
             "has_portion": False},
        ])
        self.assertEqual(ids(self.rpc.search_food_translation("äpple", "sv")),
                         [50000002])
        self.assertEqual(ids(self.rpc.search_food_translation("omena", "fi")),
                         [50000002])
        self.assertEqual(self.rpc.search_food_translation("omena", "sv"), [])
        # The one deliberate difference here: FTS5 folds diacritics and
        # Postgres's 'simple' config does not, so Postgres returns no row.
        self.assertEqual(ids(self.rpc.search_food_translation("apple", "sv")),
                         [50000002])
        # No stemming ('simple'): "Kartoffeln" is another word.
        self.assertEqual(
            ids(self.rpc.search_food_translation("kartoffeln", "de")), [1004])

    # -- portions -----------------------------------------------------------

    def test_portions_by_food_ids(self):
        rows = self.rpc.portions_by_food_ids([50000001, 1007, 1003, 1001],
                                             "de")
        self.assertEqual(rows, [
            {"food_id": 1001, "seq": 1, "label": "1 Tasse", "localized": True,
             "gram_weight": 150.0, "label_en": "1 cup"},
            {"food_id": 1007, "seq": 1, "label": "1 Tasse", "localized": True,
             "gram_weight": 200, "label_en": "1 cup"},
            {"food_id": 1007, "seq": 2, "label": "1 großes",
             "localized": True, "gram_weight": 180, "label_en": "1 large"},
            {"food_id": 50000001, "seq": 1, "label": "1 slice",
             "localized": False, "gram_weight": 30.0, "label_en": "1 slice"},
            {"food_id": 50000001, "seq": 2, "label": "1 Stück",
             "localized": True, "gram_weight": 45.0, "label_en": "1 piece"},
        ])
        # 'yields' is filtered and seq counts only what is returned.
        rows = self.rpc.portions_by_food_ids([1009], "en")
        self.assertEqual([(r["seq"], r["label"], r["localized"]) for r in rows],
                         [(1, "1 medium", False)])
        # Machine translations (every locale but the reviewed one) stay
        # hidden.
        self.assertFalse(any(r["localized"] for r in
                             self.rpc.portions_by_food_ids([1001, 1007], "pl")))

    def test_portion_labels_by_food_ids(self):
        rows = self.rpc.portion_labels_by_food_ids(
            [1002, 1001, 1003, 1006, 50000001, 424242], "de")
        self.assertEqual(rows, [
            {"food_id": 1002, "label": "1 mittelgroßes"},
            {"food_id": 1001, "label": "1 Tasse"},
        ])
        self.assertEqual(self.rpc.portion_labels_by_food_ids([1001], "pl"), [])

    def test_food_summary_by_ids(self):
        self.assertEqual(
            ids(self.rpc.food_summary_by_ids([50000003, 1001, 424242])),
            [1001, 50000003])
        self.assertEqual(
            ids(self.rpc.food_summary_by_ids([50000003, 1001], ["fineli"])),
            [50000003])

    # -- guards -------------------------------------------------------------

    def test_incomplete_review_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "portions_de.csv"
            review.write_text("unit_en,current,corrected,note\n"
                              "1 cup,1 Tasse,,\n", encoding="utf-8")
            with self.assertRaisesRegex(b.BuildError, "cannot be a complete"):
                b.build([DATA / "usda", DATA / "nordic"],
                        Path(tmp) / "x.sqlite", b.DEFAULT_MIGRATIONS,
                        reviews=[review], page_size=4096, gz=False,
                        attribution=False)

    def test_missing_measure_unit_is_refused(self):
        with self.assertRaisesRegex(b.BuildError, "measure_unit"):
            with tempfile.TemporaryDirectory() as tmp:
                b.build([DATA / "nordic"], Path(tmp) / "x.sqlite",
                        b.DEFAULT_MIGRATIONS, page_size=4096, gz=False,
                        attribution=False)

    def test_numeric_text(self):
        self.assertEqual(b.pg_numeric_text("1.50"), "1.50")
        self.assertEqual(b.pg_numeric_text(".5"), "0.5")
        self.assertEqual(b.pg_numeric_text("1e2"), "100")
        self.assertEqual(b.pg_numeric_text("-0.0"), "0.0")
        self.assertEqual(b.pg_numeric("125"), 125)
        self.assertEqual(type(b.pg_numeric("246.0")), float)


if __name__ == "__main__":
    unittest.main(verbosity=2)
