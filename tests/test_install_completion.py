"""Factory slow-install regression: elapsed time/API errors are not completion."""
from wam.runtipi import Runtipi, RuntipiError


def test_wait_for_slow_install_beyond_the_old_fifteen_minute_limit(monkeypatch):
    from wam import runtipi
    elapsed = [0]
    rt = Runtipi('http://invalid', 'test-only')
    reads = []

    def app(urn):
        reads.append(elapsed[0])
        if len(reads) == 2:
            raise RuntipiError('temporary observation failure')
        return {'app': {'status': 'installing' if len(reads) < 5 else 'running'}}

    monkeypatch.setattr(rt, 'app', app)
    monkeypatch.setattr(runtipi.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(runtipi.time, 'sleep', lambda seconds: elapsed.__setitem__(0, elapsed[0] + 600))
    assert rt.wait_status('wizard-claude:wizardapps', {'running', 'missing'}, timeout=None) == 'running'
    assert reads[-1] > 900


def test_api_error_is_not_a_removed_install(monkeypatch):
    from wam import runtipi
    rt = Runtipi('http://invalid', 'test-only')
    results = iter([RuntipiError('temporary failure'), {'app': None}])

    def app(urn):
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(rt, 'app', app)
    monkeypatch.setattr(runtipi.time, 'sleep', lambda seconds: None)
    assert rt.wait_status('wizard-claude:wizardapps', {'missing'}, timeout=None) == 'missing'
    # A false missing would have returned after consuming only the exception.
    assert next(results, 'consumed') == 'consumed'
