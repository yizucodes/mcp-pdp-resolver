"""Unit tests for ``validate_and_repair`` in ``resolver``."""

from __future__ import annotations

import pytest

from resolver import validate_and_repair


def _base(**overrides) -> dict:
    """Minimal valid recommendation dict."""
    rec: dict = {
        "gap": "missing a blazer for the conference",
        "buy": {
            "item": "Slim Blazer",
            "brand": "Bonobos",
            "price_usd": 298.0,
            "source": "bonobos.com",
            "url": "https://bonobos.com/products/slim-blazer",
        },
        "reasoning": "Versatile piece that covers two events.",
    }
    rec.update(overrides)
    return rec


class TestRequiredKeys:
    def test_missing_gap_raises(self) -> None:
        raw = _base()
        del raw["gap"]
        with pytest.raises(ValueError, match="gap"):
            validate_and_repair(raw)

    def test_missing_buy_raises(self) -> None:
        raw = _base()
        del raw["buy"]
        with pytest.raises(ValueError, match="buy"):
            validate_and_repair(raw)

    def test_missing_reasoning_raises(self) -> None:
        raw = _base()
        del raw["reasoning"]
        with pytest.raises(ValueError, match="reasoning"):
            validate_and_repair(raw)

    def test_missing_multiple_keys_reports_all(self) -> None:
        with pytest.raises(ValueError, match="required keys"):
            validate_and_repair({"gap": "x"})


class TestPriceCoercion:
    def test_float_passthrough(self) -> None:
        out = validate_and_repair(_base())
        assert out["buy"]["price_usd"] == 298.0

    def test_int_coerced_to_float(self) -> None:
        raw = _base()
        raw["buy"]["price_usd"] = 50
        out = validate_and_repair(raw)
        assert out["buy"]["price_usd"] == 50.0
        assert isinstance(out["buy"]["price_usd"], float)

    def test_string_dollar_sign_stripped(self) -> None:
        raw = _base()
        raw["buy"]["price_usd"] = "$49.99"
        out = validate_and_repair(raw)
        assert out["buy"]["price_usd"] == 49.99

    def test_string_with_comma_thousands(self) -> None:
        raw = _base()
        raw["buy"]["price_usd"] = "$1,299.00"
        out = validate_and_repair(raw)
        assert out["buy"]["price_usd"] == 1299.0

    def test_zero_price_preserved(self) -> None:
        raw = _base()
        raw["buy"]["price_usd"] = 0
        out = validate_and_repair(raw)
        assert out["buy"]["price_usd"] == 0.0

    def test_invalid_price_raises(self) -> None:
        raw = _base()
        raw["buy"]["price_usd"] = "free"
        with pytest.raises(ValueError, match="price_usd"):
            validate_and_repair(raw)

    def test_none_price_raises(self) -> None:
        raw = _base()
        raw["buy"]["price_usd"] = None
        with pytest.raises(ValueError, match="price_usd"):
            validate_and_repair(raw)


class TestBuyDefaults:
    def test_missing_buy_keys_filled(self) -> None:
        raw = _base()
        raw["buy"] = {"price_usd": 10.0}
        out = validate_and_repair(raw)
        assert out["buy"]["item"] == ""
        assert out["buy"]["brand"] == ""
        assert out["buy"]["source"] == ""
        assert out["buy"]["url"] == ""
        assert out["buy"]["price_usd"] == 10.0

    def test_empty_buy_dict_gets_defaults_and_zero_price(self) -> None:
        raw = _base()
        raw["buy"] = {}
        out = validate_and_repair(raw)
        assert out["buy"]["price_usd"] == 0.0


class TestOptionalFields:
    def test_events_covered_default(self) -> None:
        out = validate_and_repair(_base())
        assert out["events_covered"] == []

    def test_events_covered_preserved(self) -> None:
        raw = _base(events_covered=["Conference", "Dinner"])
        out = validate_and_repair(raw)
        assert out["events_covered"] == ["Conference", "Dinner"]

    def test_total_wear_days_default(self) -> None:
        out = validate_and_repair(_base())
        assert out["total_wear_days"] == 1

    def test_total_wear_days_preserved(self) -> None:
        raw = _base(total_wear_days=5)
        out = validate_and_repair(raw)
        assert out["total_wear_days"] == 5

    def test_total_wear_days_invalid_type_reset(self) -> None:
        raw = _base(total_wear_days="many")
        out = validate_and_repair(raw)
        assert out["total_wear_days"] == 1


class TestImmutability:
    def test_input_not_mutated(self) -> None:
        raw = _base()
        original_buy = dict(raw["buy"])
        validate_and_repair(raw)
        assert raw["buy"] == original_buy
        assert "events_covered" not in raw
