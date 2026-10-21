from pathlib import Path

from wam import host_dashboard as hd


def test_suite_never_touches_the_real_host_dashboard_unit():
    real = Path.home() / ".config/systemd/user" / hd.UNIT
    before = real.read_text() if real.is_file() else None
    assert hd.disable() is False  # the guard's temp unit dir is empty
    assert (real.read_text() if real.is_file() else None) == before
