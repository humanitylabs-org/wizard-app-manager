"""Pure installer transaction regressions. No root commands or host units touched."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wam import approval_custody as ac


class CustodyInstallAtomicTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('custody_installer_test', Path(__file__).parents[1] / 'scripts/install-approval-custody.py')
        assert spec is not None and spec.loader is not None
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.units = Path(self.temp.name) / 'system'
        self.units.mkdir()
        self.state = Path(self.temp.name) / 'state'
        self.state.mkdir()
        self.socket = self.units / ac.SOCKET
        self.service = self.units / ac.SERVICE
        self.dep = self.units / 'tailscaled.service.d/50-wizard-owner-approval.conf'
        self.calls = []
        self.failure_point = None
        self.active = {ac.SOCKET: 'inactive', ac.SERVICE: 'inactive'}
        self.custody = SimpleNamespace(SOCKET=ac.SOCKET, SERVICE=ac.SERVICE, SOCKET_TEXT=ac.SOCKET_TEXT,
                                      TAILSCALE_TEXT=ac.TAILSCALE_TEXT,
                                      require_socket_custody=Mock(side_effect=self.socket_check),
                                      require_custody=Mock(side_effect=self.final_check))
        for target, value in [('UNITS', self.units), ('protected', lambda p: None),
                              ('run', self.run_command), ('routes', lambda: [])]:
            p = patch.object(self.installer, target, value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(self.installer.os, 'sync')
        p.start(); self.addCleanup(p.stop)
        self.user = SimpleNamespace(pw_uid=1234, pw_gid=1234, pw_dir='/var/lib/synthetic-manager')

    def link(self, unit, target):
        p = self.units / target / unit
        p.parent.mkdir(exist_ok=True)
        if not p.is_symlink(): p.symlink_to(self.units / unit)
        return p

    def run_command(self, *args):
        self.calls.append(args)
        if args[:2] == ('systemctl', 'show'):
            unit = args[2]
            if len(args) > 3: return SimpleNamespace(stdout='loaded\n')
            exists = (self.units / unit).exists() or unit == 'tailscaled.service'
            requires = ac.SOCKET if unit == 'tailscaled.service' and self.dep.exists() else ''
            return SimpleNamespace(stdout=f'LoadState={"loaded" if exists else "not-found"}\nActiveState={self.active.get(unit, "active")}\nRequires={requires}\n')
        if args[:2] == ('systemctl', 'enable'):
            unit = args[-1]
            self.link(unit, 'sockets.target.wants' if unit == ac.SOCKET else 'multi-user.target.wants')
            if unit == ac.SOCKET:
                # Acquisition must happen BEFORE any dependency commitment.
                if self.custody.require_custody.call_count == 0: self.assertFalse(self.dep.exists())
                if self.failure_point == 'acquire': raise RuntimeError('occupied')
                self.active[unit] = 'active'
            elif self.failure_point == 'enable-worker': raise RuntimeError('enable-worker')
        if args[:2] == ('systemctl', 'stop'):
            self.assertNotEqual(args[-1], 'tailscaled.service')
            if args[-1] == ac.SOCKET: self.assertFalse(self.dep.exists())
            self.active[args[-1]] = 'inactive'
        return SimpleNamespace(stdout='')

    def socket_check(self):
        self.assertEqual(self.active[ac.SOCKET], 'active')
        if self.failure_point == 'verify-socket': raise RuntimeError('verify-socket')
        return 42

    def final_check(self):
        if self.failure_point == 'verify-final': raise RuntimeError('verify-final')
        return 42

    def apply(self):
        self.installer.apply('install', self.user, 'synthetic-manager', self.state, Path('/opt/synthetic'), self.custody)

    def snapshot(self):
        return {str(p.relative_to(self.units)): ('link', p.readlink()) if p.is_symlink() else ('file', p.read_bytes(), p.stat().st_mode & 0o777)
                for p in self.units.rglob('*') if p.is_file() or p.is_symlink()}

    def seed_contract(self):
        self.socket.write_text(ac.SOCKET_TEXT)
        self.service.write_text('preexisting worker')
        self.dep.parent.mkdir()
        self.dep.write_text(ac.TAILSCALE_TEXT)
        self.link(ac.SOCKET, 'sockets.target.wants')
        self.link(ac.SERVICE, 'multi-user.target.wants')
        self.active[ac.SOCKET] = 'active'

    def test_first_install_success_acquires_before_dependency(self):
        self.apply()
        self.assertEqual(self.dep.read_text(), ac.TAILSCALE_TEXT)
        self.custody.require_socket_custody.assert_called_once()
        self.custody.require_custody.assert_called_once()
        self.assertEqual(self.active[ac.SOCKET], 'active')

    def test_occupied_first_install_restores_files_and_enablement(self):
        self.failure_point = 'acquire'
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'occupied'): self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.dep.parent.exists())

    def test_socket_verification_failure_rolls_back_first_install(self):
        self.failure_point = 'verify-socket'
        with self.assertRaisesRegex(RuntimeError, 'verify-socket'): self.apply()
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(self.active[ac.SOCKET], 'inactive')

    def test_postdependency_daemon_reload_failure_restores_first_install(self):
        real_run = self.installer.run
        failed = False
        def reload_failure(*args):
            nonlocal failed
            if args == ('systemctl', 'daemon-reload') and self.dep.exists() and not failed:
                failed = True
                raise RuntimeError('postdependency reload failed')
            return real_run(*args)
        with patch.object(self.installer, 'run', side_effect=reload_failure):
            with self.assertRaisesRegex(RuntimeError, 'postdependency reload failed'): self.apply()
        self.assertTrue(failed)
        self.assertEqual(self.snapshot(), {})

    def test_initial_write_failure_creates_no_contract(self):
        with patch.object(self.installer, 'write', side_effect=OSError('write unavailable')):
            with self.assertRaisesRegex(OSError, 'write unavailable'): self.apply()
        self.assertEqual(self.snapshot(), {})

    def test_postdependency_enable_failure_rolls_back_first_install(self):
        self.failure_point = 'enable-worker'
        with self.assertRaisesRegex(RuntimeError, 'enable-worker'): self.apply()
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(self.active[ac.SOCKET], 'inactive')

    def test_postdependency_readback_failure_rolls_back_first_install(self):
        self.failure_point = 'verify-final'
        with self.assertRaisesRegex(RuntimeError, 'verify-final'): self.apply()
        self.assertEqual(self.snapshot(), {})

    def test_first_install_preserves_preexisting_enabled_active_worker(self):
        self.service.write_text('original worker\n'); self.service.chmod(0o640)
        self.link(ac.SERVICE, 'multi-user.target.wants')
        self.link(ac.SERVICE, 'custom.target.wants')
        self.active[ac.SERVICE] = 'active'
        before = self.snapshot(); self.failure_point = 'verify-final'
        with self.assertRaises(RuntimeError): self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.active[ac.SERVICE], 'active')
        self.assertNotIn(('systemctl', 'stop', ac.SERVICE), self.calls)

    def test_absent_route_existing_contract_failure_keeps_socket_and_dependency(self):
        self.seed_contract(); self.failure_point = 'enable-worker'
        with self.assertRaisesRegex(RuntimeError, 'enable-worker'): self.apply()
        self.assertEqual(self.socket.read_text(), ac.SOCKET_TEXT)
        self.assertEqual(self.dep.read_text(), ac.TAILSCALE_TEXT)
        self.assertEqual(self.active[ac.SOCKET], 'active')
        self.assertFalse(any(c[:2] == ('systemctl', 'stop') for c in self.calls))

    def test_routed_existing_failure_keeps_custody(self):
        self.seed_contract(); self.failure_point = 'verify-final'
        # The initial readback succeeds; the final readback fails.
        self.custody.require_custody.side_effect = [42, RuntimeError('verify-final')]
        with patch.object(self.installer, 'routes', return_value=['fixture.example.ts.net:8446']):
            with self.assertRaisesRegex(RuntimeError, 'verify-final'): self.apply()
        self.assertTrue(self.dep.exists() and self.socket.exists())
        self.assertEqual(self.active[ac.SOCKET], 'active')
        self.assertFalse(any(c[:2] == ('systemctl', 'stop') for c in self.calls))

    def test_broken_existing_contract_refuses_before_writes(self):
        self.seed_contract(); before = self.snapshot()
        self.custody.require_custody.side_effect = ValueError('custody broken')
        with self.assertRaisesRegex(ValueError, 'custody broken'): self.apply()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(c[:2] == ('systemctl', 'enable') for c in self.calls))

    def test_untrusted_origin_config_does_not_select_upgrade_mode(self):
        (self.state / 'approval-origin.json').write_text('{"custody":1,"origin":"https://forged:8446"}')
        self.failure_point = 'acquire'
        with self.assertRaisesRegex(RuntimeError, 'occupied'): self.apply()
        self.assertEqual(self.snapshot(), {})

    def test_loaded_contract_without_local_fragment_is_not_first_install(self):
        real_loaded = self.installer.loaded
        with patch.object(self.installer, 'loaded', side_effect=lambda unit: {'LoadState': 'loaded'} if unit == ac.SOCKET else real_loaded(unit)):
            self.custody.require_custody.side_effect = ValueError('loaded contract broken')
            with self.assertRaisesRegex(ValueError, 'loaded contract broken'): self.apply()
        self.assertEqual(self.snapshot(), {})
        self.assertFalse(any(c[:2] == ('systemctl', 'enable') for c in self.calls))

    def test_status_failure_does_not_write_or_assume_first_install(self):
        before = self.snapshot()
        with patch.object(self.installer, 'routes', side_effect=RuntimeError('status unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'status unavailable'): self.apply()
        self.assertEqual(self.snapshot(), before)

    def test_show_missing_only_accepts_explicit_not_found(self):
        with patch.object(self.installer, 'run', side_effect=subprocess.CalledProcessError(1, ['systemctl'], output='LoadState=not-found\n')):
            self.assertEqual(self.installer.loaded(ac.SOCKET)['LoadState'], 'not-found')
        with patch.object(self.installer, 'run', side_effect=subprocess.CalledProcessError(1, ['systemctl'], output='')):
            with self.assertRaises(subprocess.CalledProcessError): self.installer.loaded(ac.SOCKET)


if __name__ == '__main__':
    unittest.main()
