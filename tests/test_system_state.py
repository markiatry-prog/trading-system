"""The kill switch must fail closed for every way "cannot confirm normal"
can happen -- not just the tidy ones. Each case below is a real failure
mode, not a hypothetical."""
import pytest
from trading_system.system_state import (
    Paused, SystemState, check_system_state, is_permitted, require_normal,
)


class _Reader:
    def __init__(self, value=None, raises=None):
        self._value, self._raises = value, raises
    def read_system_state(self):
        if self._raises:
            raise self._raises
        return self._value


def test_normal_is_the_only_permitted_state():
    assert check_system_state(_Reader("normal")) is SystemState.NORMAL
    assert is_permitted(_Reader("normal")) is True


def test_paused_blocks():
    assert check_system_state(_Reader("paused")) is SystemState.PAUSED
    assert is_permitted(_Reader("paused")) is False


@pytest.mark.parametrize("reader,label", [
    (_Reader(raises=ConnectionError("unreachable")), "unreachable database"),
    (_Reader(raises=PermissionError("permission denied for table system_state")), "insufficient permission"),
    (_Reader(raises=TimeoutError("statement timeout")), "timeout"),
    (_Reader(raises=RuntimeError("system_state has no row")), "missing row"),
    (_Reader(None), "null state"),
    (_Reader(""), "empty state"),
    (_Reader("NORMAL"), "wrong case"),
    (_Reader(" normal "), "padded value"),
    (_Reader("enabled"), "unrecognized value"),
    (_Reader(object()), "non-string value"),
    (_Reader(0), "falsy non-string value"),
])
def test_every_inability_to_confirm_normal_fails_closed(reader, label):
    assert check_system_state(reader) is SystemState.PAUSED, label
    assert is_permitted(reader) is False, label


def test_check_never_raises_whatever_the_reader_does():
    class _Hostile:
        def read_system_state(self):
            raise BaseException("not even an Exception subclass")
    try:
        # BaseException is deliberately NOT caught -- catching it would
        # swallow KeyboardInterrupt/SystemExit. Documented, not accidental.
        check_system_state(_Hostile())
    except BaseException as exc:
        assert not isinstance(exc, Exception)


def test_require_normal_raises_when_not_permitted():
    with pytest.raises(Paused):
        require_normal(_Reader("paused"))
    with pytest.raises(Paused):
        require_normal(_Reader(raises=ConnectionError()))


def test_require_normal_returns_when_permitted():
    assert require_normal(_Reader("normal")) is SystemState.NORMAL


def test_module_exposes_no_way_to_write_state():
    """Nothing in this module may change the switch. Turning the system
    back on is a control-plane act, not something a component can do to
    itself."""
    import trading_system.system_state as mod
    exported = [n for n in dir(mod) if not n.startswith("_")]
    for name in exported:
        assert not any(w in name.lower() for w in ("set_", "write", "update", "resume", "unpause"))
