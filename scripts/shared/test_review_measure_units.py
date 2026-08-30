#!/usr/bin/env python3
"""Checks the rule review_measure_units.py exists to enforce.

Promoting a locale to 'verified' is a claim that a person read the whole
list. The only thing standing between that claim and a half-finished CSV is
`plan`, so it is worth testing on its own — and it can be, because it
decides everything without a database.

Run: python3 scripts/shared/test_review_measure_units.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_measure_units import DELETE_MARKER, IncompleteReview, plan  # noqa: E402

FAILURES: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"  ok   {name}")
    except AssertionError as exc:
        FAILURES.append(name)
        print(f"  FAIL {name}: {exc}")


def blank_review_promotes_everything_unchanged() -> None:
    # An empty `corrected` column means "the machine got this right", which
    # is the common case and must not be mistaken for an unreviewed row.
    corrections, deletions = plan({"cup": "", "slice": ""}, {"cup", "slice"})
    assert corrections == {}, corrections
    assert deletions == [], deletions


def corrections_are_collected() -> None:
    corrections, deletions = plan(
        {"cup": "Tasse", "slice": ""}, {"cup", "slice"})
    assert corrections == {"cup": "Tasse"}, corrections
    assert deletions == [], deletions


def the_delete_marker_removes_a_row() -> None:
    # "no word fits" has to be expressible, or a reviewer's only options are
    # to approve a bad guess or leave the whole locale unverified.
    corrections, deletions = plan(
        {"cup": "Tasse", "rack": DELETE_MARKER}, {"cup", "rack"})
    assert corrections == {"cup": "Tasse"}, corrections
    assert deletions == ["rack"], deletions


def whitespace_is_not_a_correction() -> None:
    corrections, _ = plan({"cup": "   ".strip()}, {"cup"})
    assert corrections == {}, corrections


def a_missing_row_refuses_the_whole_promotion() -> None:
    # The point of the script. A CSV that lost rows must not be able to
    # promote a locale, because afterwards nothing distinguishes it from a
    # complete one.
    try:
        plan({"cup": ""}, {"cup", "slice", "leg"})
    except IncompleteReview as exc:
        assert "slice" in str(exc) and "leg" in str(exc), str(exc)
        assert "2 of 3" in str(exc), str(exc)
    else:
        raise AssertionError("an incomplete CSV was accepted")


def a_long_missing_list_is_truncated_but_counted() -> None:
    in_db = {f"unit{i}" for i in range(40)}
    try:
        plan({}, in_db)
    except IncompleteReview as exc:
        assert "40 of 40" in str(exc), str(exc)
        assert "..." in str(exc), str(exc)
    else:
        raise AssertionError("an empty CSV was accepted")


def an_unknown_unit_is_refused() -> None:
    # A hand-edited CSV can gain a typo'd row; silently ignoring it would
    # make the reviewer think they corrected something they did not.
    try:
        plan({"cup": "", "cupp": "Tasse"}, {"cup"})
    except IncompleteReview as exc:
        assert "cupp" in str(exc), str(exc)
    else:
        raise AssertionError("an unknown unit was accepted")


def main() -> None:
    print("plan() — the completeness guard")
    for fn in (
        blank_review_promotes_everything_unchanged,
        corrections_are_collected,
        the_delete_marker_removes_a_row,
        whitespace_is_not_a_correction,
        a_missing_row_refuses_the_whole_promotion,
        a_long_missing_list_is_truncated_but_counted,
        an_unknown_unit_is_refused,
    ):
        check(fn.__name__.replace("_", " "), fn)
    if FAILURES:
        sys.exit(f"\n{len(FAILURES)} failed: {FAILURES}")
    print("\nall passed")


if __name__ == "__main__":
    main()
