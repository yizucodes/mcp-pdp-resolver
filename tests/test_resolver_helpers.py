"""Unit tests for pure helpers in ``resolver`` (no network)."""

from __future__ import annotations

import json

import pytest

from resolver import (
    _clean_item_name,
    _domain_for_retailer,
    is_plp_url,
    _short_item_name,
    strip_tracking_params,
    extract_json,
)


@pytest.mark.parametrize(
    ("url", "expected_plp"),
    [
        ("https://shop.example/category/men/shirts", True),
        ("https://shop.example/c/men/shirts", True),
        ("https://shop.example/search?q=shirt", True),
        ("https://shop.example/products/slim-shirt-navy", False),
        ("https://shop.example/item/12345", False),
        ("", False),
    ],
)
def testis_plp_url(url: str, expected_plp: bool) -> None:
    assert is_plp_url(url) is expected_plp


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://x.com/p/1?utm_source=ig&gclid=abc&ref=1&color=navy",
            "https://x.com/p/1?color=navy",
        ),
        ("https://x.com/p/1", "https://x.com/p/1"),
    ],
)
def teststrip_tracking_params(url: str, expected: str) -> None:
    assert strip_tracking_params(url) == expected


@pytest.mark.parametrize(
    ("raw", "expected_core"),
    [
        ("Navy Stretch Chinos, Slim Fit", "Stretch Chinos"),
        ("Blue Oxford Shirt — Medium", "Oxford Shirt"),
    ],
)
def test_clean_item_name_truncates_after_comma(raw: str, expected_core: str) -> None:
    out = _clean_item_name(raw)
    assert expected_core in out


def test_short_item_name_prefers_clothing_term() -> None:
    assert "chinos" in _short_item_name(_clean_item_name("navy stretch chinos slim"))


@pytest.mark.parametrize(
    ("current_url", "source", "expected_substring"),
    [
        ("", "Bonobos", "bonobos.com"),
        ("https://www.gap.com/foo", "", "gap.com"),
        ("", "made up retailer xyz", "xyz.com"),
    ],
)
def test_domain_for_retailer(
    current_url: str, source: str, expected_substring: str
) -> None:
    assert expected_substring in _domain_for_retailer(current_url, source)


@pytest.mark.parametrize(
    ("text", "expected_name"),
    [
        ('{"a": 1}', None),
        ('```json\n{"name": "x"}\n```', "x"),
        ('Here you go:\n{"name": "y"}\nThanks', "y"),
    ],
)
def test_extract_json(text: str, expected_name: str | None) -> None:
    data = extract_json(text)
    if expected_name is None:
        assert data == {"a": 1}
    else:
        assert data.get("name") == expected_name


def test_extract_json_invalid_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        extract_json("no json here")
