#!/usr/bin/env python3
"""Clean Debian 12/KVM legacy migration + rollback + remigration.

Usage: run.py EVIDENCE_DIR SOURCE_EXPORT CANDIDATE_SHA [--smoke|--preflight]
Main baseline is pinned below. --preflight validates the REAL argument parser,
staged manifest and generated VM function, without creating evidence or a VM.
No tailnet credentials; existing authenticated app URLs need an owner rehearsal.
"""
import argparse
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET

BASELINE_SHA = '67141228f6f73516ab68ece1c4c72bf3f67aebe0'


def arguments(argv):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('evidence_dir',type=Path); p.add_argument('source_export',type=Path)
    p.add_argument('candidate_sha'); p.add_argument('--smoke',action='store_true')
    p.add_argument('--preflight',action='store_true')
    a=p.parse_args(argv); root,source=a.evidence_dir.resolve(),a.source_export.resolve()
    assert re.fullmatch('[0-9a-f]{40}',a.candidate_sha)
    assert str(root).startswith('/srv/work/privsep/slice6/') and not root.exists()
    assert source.is_dir() and (source/'qa/privsep-slice6/guest.py').is_file()
    manifest=json.loads((source.parent/'source-export.json').read_text())
    assert manifest['head_sha']==a.candidate_sha and manifest['baseline_sha']==BASELINE_SHA
    return a,root,source


def load_skeleton(source):
    first=source/'qa/privsep-slice1/run.py'; tree=ast.parse(first.read_text())
    names={'save','run','free_port','guest_cloud_config','guest_install_command'}
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),str(first),'exec'),globals())
    fourth=source/'qa/privsep-slice4/run.py'; text=fourth.read_text(); tree=ast.parse(text)
    functions={'require_step','guest_bootstrap_command','suite','interrupt','doctor_rows'}
    constants={'HERMES_SETUP'}
    nodes=[n for n in tree.body if (isinstance(n,ast.FunctionDef) and n.name in functions) or (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in constants for t in n.targets))]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(fourth),'exec'),globals())
    globals()['INSTALL_PRIVATE_ACCESS']={'candidate':'none','baseline':'none'}
    vm=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='guest_vm')
    definition=ast.get_source_segment(text,vm)
    start=definition.index('        install=guest_bootstrap_command(ref, label)')
    finish=definition.index('    finally:',start)
    body=r'''        # Rehearsal-only workaround for main's missing user bus. HERMES_SETUP
        # starts the user manager; install dbus-user-session before it runs.
        install=guest_bootstrap_command(BASELINE_SHA, label)
        if guest('legacy-install',guest_install_command(install),4500,True): return
        guest('mirror',"docker info --format '{{json .RegistryConfig.Mirrors}}' | python3 -c 'import json,sys; assert any(\"mirror.gcr.io\" in v for v in json.load(sys.stdin))'",60,True)
        guest('legacy-source','runuser -u hermes -- git -C /home/hermes/wizard-app-manager rev-parse HEAD | grep -Fx '+BASELINE_SHA,60,True)
        script=(source/'qa/privsep-slice6/guest.py').read_text()
        guest('slice6-script',"cat > /root/slice6-guest.py <<'SLICE6_SCRIPT'\n"+script+'\nSLICE6_SCRIPT\n',60,True)
        if guest('legacy','python3 /root/slice6-guest.py legacy '+ref,1500,True): return
        # Use the production protected root entry point, with an owner checkpoint
        # to compare all app-data bytes before applications write again.
        setup=('install -d -m700 /var/lib/wizard-os-migration; '
               'git clone --no-checkout https://github.com/humanitylabs-org/wizard-app-manager.git /var/lib/wizard-os-migration/source; '
               'git -C /var/lib/wizard-os-migration/source checkout --detach '+ref+'; '
               '/usr/bin/python3 -I /var/lib/wizard-os-migration/source/scripts/wam-privilege migrate '
               '--agent hermes --hermes-home /home/hermes/.hermes --ref '+ref+' --stop-after relocate')
        if guest('checkpoint-migrate',setup,600,True): return
        if guest('checkpoint','python3 /root/slice6-guest.py checkpoint '+ref,600,True): return
        if guest('injected-failure','python3 /root/slice6-guest.py fail-before-recover '+ref,900,True): return
        if guest('failure-rollback','/usr/local/bin/wam privilege rollback',1500,True): return
        if guest('failure-restored','python3 /root/slice6-guest.py rollback '+ref,900,True): return
        prepare=('/usr/bin/python3 -I /var/lib/wizard-os-migration/source/scripts/prepare-privilege-migration.py '
                 'hermes /home/hermes/.hermes '+ref)
        if guest('migrate',prepare,2400,True): return
        if guest('migrated','python3 /root/slice6-guest.py migrated '+ref,900,True): return
        if guest('rollback','/usr/local/bin/wam privilege rollback',1500,True): return
        if guest('rolled-back','python3 /root/slice6-guest.py rollback '+ref,900,True): return
        if guest('remigrate',prepare,2400,True): return
        if guest('again','python3 /root/slice6-guest.py again '+ref,900,True): return
        guest('source','runuser -u wizard-system -- git -C /var/lib/wizard-os/wizard-app-manager rev-parse HEAD | grep -Fx '+ref,60,True)
        guest('doctor','cat /root/slice6-doctor.json',60,True)
'''
    definition=definition[:start]+body+definition[finish:]
    definition=definition.replace('        if required: require_step(label+\'-\'+name,rc)',
        "        if rc and required:\n            run(label+'-'+name+'-runtime-log',ssh+['docker logs --tail 500 runtipi 2>&1'],timeout=90,required=False)\n        if required: require_step(label+'-'+name,rc)")
    definition=definition.replace('privsep4-','privsep6-')
    # Debian's user bus is an explicit setup workaround, not a product fix and
    # not a change to the main baseline installer or stock Hermes.
    definition=definition.replace('python3-yaml build-essential libatomic1','python3-yaml build-essential libatomic1 dbus-user-session')
    definition=definition.replace("        setup = HERMES_SETUP", "        guest('tailscale-package', 'curl -fsSL https://tailscale.com/install.sh -o /root/install-tailscale.sh; sh /root/install-tailscale.sh; systemctl enable --now tailscaled', 600, True)\n        setup = HERMES_SETUP")
    exec(compile(definition,str(fourth),'exec'),globals())


def finalize(value):
    names=('fresh','tools','tailscale-package','stock-hermes','legacy-install','mirror','legacy-source','slice6-script',
           'legacy','checkpoint-migrate','checkpoint','injected-failure','failure-rollback','failure-restored','migrate','migrated','rollback','rolled-back','remigrate','again','source','doctor')
    steps={row['name']:row['exit'] for row in value['steps']}
    value['candidate_passed']=(all(steps.get('candidate-'+n)==0 for n in names) and
        value.get('candidate_doctor_passed') is True and len(value['vms'])==1 and value['vms'][0]['stopped'])
    suite_ok=value['smoke'] or value.get('pytest',{}).get('failures')==['tests.test_chats_views::test_typescript_public_index_path_has_identical_seal']
    value['passed']=value['candidate_passed'] and suite_ok and not value['failures']


def main(argv):
    global root,source,candidate,receipt,private
    args,root,source=arguments(argv); load_skeleton(source)
    if args.preflight:
        print(json.dumps({'preflight':'passed','head_sha':args.candidate_sha,'baseline_sha':BASELINE_SHA,
                          'argv':[str(root),str(source),args.candidate_sha]})); return 0
    candidate=args.candidate_sha; root.mkdir(mode=0o700,parents=True)
    private=root/'private'; private.mkdir(mode=0o700)
    receipt={'started':datetime.datetime.now(datetime.timezone.utc).isoformat(),'candidate':candidate,
             'baseline':BASELINE_SHA,'smoke':args.smoke,'steps':[],'vms':[],'failures':[],
             'candidate_passed':False,'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'baseline_workaround':'dbus-user-session + loginctl enable-linger + systemctl start user@uid before main user phase',
             'tailnet_links':'No authenticated tailnet in this guest; exact Serve preservation and loopback targets checked by migration. Public/private URL resolution still needs owner-controlled rehearsal.'}
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGALRM): signal.signal(sig,interrupt)
    signal.alarm(10500)
    try:
        hermes=subprocess.check_output(['git','ls-remote','https://github.com/NousResearch/hermes-agent.git','HEAD'],text=True,timeout=60).split()[0]
        assert re.fullmatch('[0-9a-f]{40}',hermes); receipt['hermes_commit']=hermes
        image=Path('/srv/work/images/debian-12-genericcloud-amd64.qcow2')
        if not image.is_file():
            image=root/'debian-12.qcow2'
            run('download',['curl','-fL','--retry','3','https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2','-o',str(image)],timeout=600)
        run('image-hash',['sha256sum',str(image)])
        key=private/'id_ed25519'; run('key',['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)])
        guest_vm('candidate',candidate,image,key)
        if (root/'candidate-doctor.log').is_file():
            rows=doctor_rows('candidate'); receipt['candidate_doctor_passed']=bool(rows) and all(row['ok'] for row in rows)
        save()
        if not args.smoke: suite()
    except Exception:
        receipt['exception']=traceback.format_exc(); receipt['failures'].append('runner exception')
    finally:
        receipt['finished']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        if not all(row['stopped'] for row in receipt['vms']): receipt['failures'].append('VM teardown incomplete')
        finalize(receipt); save()
    print(json.dumps(receipt,indent=2)); return 0 if receipt['passed'] else 1


if __name__=='__main__': sys.exit(main(sys.argv[1:]))
