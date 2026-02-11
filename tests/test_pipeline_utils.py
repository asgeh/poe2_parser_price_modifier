from poe2_price_modifier.utils import (
    expand_bracket_variants,
    extract_mod_lines,
    normalize_mod_text,
    parse_price,
)


def test_normalize_mod_text():
    assert normalize_mod_text(" +12 to Strength ") == "# to strength"


def test_expand_bracket_variants_contains_both_options():
    variants = expand_bracket_variants("adds [fire|cold] damage")
    assert "adds fire damage" in variants
    assert "adds cold damage" in variants


def test_extract_mod_lines_deduplicates_from_multiple_sources():
    item = {
        "explicitMods": ["A", "A"],
        "extended": {"mods": {"explicit": ["B"], "desecrated": ["C"]}},
    }
    assert extract_mod_lines(item) == ["A", "B", "C"]


def test_parse_price_currency_filtering():
    listing = {"price": {"amount": 5, "currency": "divine-orb"}}
    assert parse_price(listing, "divine") == 5.0
    assert parse_price(listing, "exalted") is None
