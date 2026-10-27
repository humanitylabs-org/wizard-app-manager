# Slice 1 acceptance: fresh install without passwordless sudo

Run: forum KVM, two clean Debian 12 genericcloud guests (same image sha512, same Docker registry mirror),
baseline main 513fb156 vs candidate 1eb3be3d. Evidence dir on forum: /srv/work/privsep/slice1/20261027T161948Z.
Both VMs stopped (qemu exit 0).

| Check | Baseline (main) | Candidate |
|---|---|---|
| `sudo -l -U wizard` | NOPASSWD: ALL | only `/usr/local/libexec/wizard-os/os-patch ""` |
| `wizard` general sudo | allowed | denied (password required) |
| os-patch via sudo / with arguments | n/a | runs / refused by sudo and by helper |
| Old blanket grant on installer rerun | n/a | removed |
| Tailscale operator | not wizard | `wizard` |
| Agent container (uid 1000) | no socket, no sudo | no socket, no sudo; mounts /opt/data, /opt/wizard-builder, /wizard, /wizard-requests only |
| `wam doctor` | PASS | PASS (same rows) |
| Builder bundle doctor | PASS | PASS |
| Running containers | 24 lines | same set |
| Install outcome | 2 known issues | same 2 known issues (troubleshooter not in public index; builder mount adopt pending restart) |
| pytest | 1416, 1 failure | 1437, same 1 pre-existing failure |
| New audit tests on main | fail (as intended) | pass |

Not proven: authenticated app-link publication (no tailnet login in the guests); operator setting verified instead.
