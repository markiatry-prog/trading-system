from trading_system.health import report


class _Reader:
    def __init__(self, value=None, raises=None):
        self._value, self._raises = value, raises
    def read_system_state(self):
        if self._raises:
            raise self._raises
        return self._value


def test_normal_is_ready():
    h = report(_Reader("normal"))
    assert (h.reachable, h.permitted, h.ready, h.state) == (True, True, True, "normal")


def test_paused_is_reachable_and_healthy_but_not_permitted():
    """A deliberate stop must not look like an outage."""
    h = report(_Reader("paused"))
    assert h.reachable is True and h.permitted is False and h.ready is False
    assert h.state == "paused"


def test_unreachable_database_is_reported_and_fails_closed():
    h = report(_Reader(raises=ConnectionError("host unreachable")))
    assert h.reachable is False and h.permitted is False
    assert h.state == "paused"


def test_detail_never_leaks_the_exception_message():
    secret = "postgresql://user:hunter2@db.internal:5432/postgres"
    h = report(_Reader(raises=ConnectionError(secret)))
    assert h.detail == "ConnectionError"
    assert "hunter2" not in str(h.as_dict())


def test_report_never_raises():
    class _Hostile:
        def read_system_state(self):
            raise Exception("boom")
    assert report(_Hostile()).ready is False
