import pytest
from trading_system import env


def test_strips_wrapping_quotes_and_whitespace(monkeypatch):
    monkeypatch.setenv("TRADING_X", '  "value"  ')
    assert env.get("TRADING_X") == "value"
    monkeypatch.setenv("TRADING_X", "'value'")
    assert env.get("TRADING_X") == "value"


def test_blank_is_treated_as_absent(monkeypatch):
    monkeypatch.setenv("TRADING_X", "   ")
    assert env.get("TRADING_X") is None
    assert env.get("TRADING_X", "fallback") == "fallback"


def test_require_names_the_variable_but_never_its_value(monkeypatch):
    monkeypatch.delenv("TRADING_SECRET", raising=False)
    with pytest.raises(env.MissingSetting) as e:
        env.require("TRADING_SECRET")
    assert "TRADING_SECRET" in str(e.value)


def test_unbalanced_quotes_are_left_alone(monkeypatch):
    monkeypatch.setenv("TRADING_X", '"value')
    assert env.get("TRADING_X") == '"value'
