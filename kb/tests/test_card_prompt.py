"""The card prompt names the make it is baked for and no other."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "card_baking"))

import bake_cards  # noqa: E402

MAKES = ["Subaru", "Chevrolet", "Toyota", "Ford"]


def test_prompt_names_only_its_make():
    for make in MAKES:
        text = bake_cards.system_prompt(make)
        assert make in text
        for other in MAKES:
            if other != make:
                assert other not in text, (make, other)
        assert "{make}" not in text


def test_forum_counts_key_is_make_neutral():
    counts, picked = bake_cards.forum_counts(None, "P0420", "Tahoe", 2003, 2003, 0)
    assert set(counts) == {"this_car", "all_make", "ranked"}
    assert picked == []
