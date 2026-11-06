# needrestart security backports

EXTEND: the existing `wam/os_patch.py` restart-detection gate; no new scanner or privileged lane.

`needrestart --version` reports upstream 3.6 even when an LTS package contains the November 2024 local-root fixes. The helper accepts upstream 3.8+ as before, or the following exact distro/revision families with both packages installed and `dpkg --compare-versions` at or above the fixed floors:

| Distribution | needrestart | libmodule-scandeps-perl |
| --- | --- | --- |
| Ubuntu 24.04 | 3.6-7ubuntu4.3 | 1.35-1ubuntu0.24.04.1 |
| Debian 12 | 3.6-4+deb12u2 | 1.31-2+deb12u1 |

Sources:
- [USN-7117-1](https://ubuntu.com/security/notices/USN-7117-1) covers CVE-2024-48990, CVE-2024-48991, CVE-2024-48992, CVE-2024-11003 and CVE-2024-10224, and names both Ubuntu floors.
- [DSA-5815-1](https://www.debian.org/security/2024/dsa-5815) fixes the four needrestart CVEs in 3.6-4+deb12u2.
- [DSA-5816-1](https://www.debian.org/security/2024/dsa-5816) fixes ScanDeps CVE-2024-10224 in 1.31-2+deb12u1.

Only numeric security updates within those exact revision families are accepted by the backport exception. Unknown distro IDs/releases, custom suffixes, epochs, pre-releases, unconfigured/missing packages, and query/comparison failures stay report-only. `ID_LIKE` is not proof of a backport. The scanner still runs only in list mode (`-b -r l`); this change grants no new restart/upgrade/reboot permission.

Regression coverage: `tests/test_needrestart_backports.py` exercises both fixed floors, numeric revision ordering, vulnerable and mismatched versions, metadata failures, and the invariant that a rejected scanner is never invoked in scan mode.
