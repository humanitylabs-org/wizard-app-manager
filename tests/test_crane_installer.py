"""Platform selection must never install a Darwin binary on the Linux runner."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('crane_installer', Path(__file__).parents[1] / 'tools/install_crane.py')
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class CranePlatform(unittest.TestCase):
    def test_linux_x64_is_checksum_pinned(self):
        url, checksum = installer.release('Linux', 'x86_64')
        self.assertTrue(url.endswith('go-containerregistry_Linux_x86_64.tar.gz'))
        self.assertEqual(checksum, 'c1d593d01551f2c9a3df5ca0a0be4385a839bd9b86d4a76e18d7b17d16559127')

    def test_mac_arm_still_supported(self):
        url, checksum = installer.release('Darwin', 'arm64')
        self.assertTrue(url.endswith('go-containerregistry_Darwin_arm64.tar.gz'))
        self.assertEqual(checksum, 'ae30b28498de3e1ac51a37042eab46116ec70d86aa9d10431a1fbde35b9918b9')

    def test_unsupported_platform_fails_closed(self):
        with self.assertRaises(ValueError):
            installer.release('Linux', 'unknown')
