#!/usr/bin/env python3
"""Explicit, bounded real-host acceptance. Run ONLY on authorized disposable demo.
Receipts never contain credential values. Backups stay root-only on that host.
Not a production installer. Each destructive phase requires --authorize-reset.
"""
import argparse, datetime, hashlib, json, os, pathlib, subprocess, sys, time
P = pathlib.Path(os.environ.get('WIZARD_ACCEPTANCE_EVIDENCE_DIR', '/root/wizard-acceptance-20261019'))
if P.parent != pathlib.Path('/root') or not P.name.startswith('wizard-acceptance-'):
    raise ValueError('Evidence must stay in a root-private wizard-acceptance-* directory')
OWNER = pathlib.Path('/home/dana')

def command(args, name, timeout=600, check=True):
    with (P / (name + '.log')).open('a') as log:
        result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f'{name} exited {result.returncode}; see {P}/{name}.log')
    return result.returncode

def output(args):
    return subprocess.check_output(args, text=True, timeout=30)

def receipt(name, data):
    data.update(at=datetime.datetime.now(datetime.timezone.utc).isoformat(), case=name)
    (P / (name + '.json')).write_text(json.dumps(data, indent=2) + '\n')
    with (P/'receipts.jsonl').open('a') as history:
        history.write(json.dumps(data)+'\n')
    print(json.dumps(data), flush=True)

def owner_env_hash():
    # Wizard-owned link credentials may be newly generated after an intentional unlink/reset.
    # All original provider values must remain unchanged; do not serialize or print those values.
    rows={}
    for line in (OWNER/'.hermes/.env').read_text().splitlines():
        k, sep, value=line.partition('=')
        if sep and not k.lstrip().startswith('#') and k.strip() not in {'API_SERVER_KEY','WIZARD_MCP_TOKEN'}:
            rows[k]=value
    return hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()

def snapshot():
    paths = [OWNER/'.hermes/config.yaml', OWNER/'.hermes/.env', OWNER/'.hermes/auth.json', OWNER/'.hermes/SOUL.md']
    for d in ['.hermes/memories', '.hermes/skills/studio', 'notes']:
        paths += sorted(p for p in (OWNER/d).rglob('*') if p.is_file())
    return {str(p.relative_to(OWNER)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('phase', choices=['baseline','no-docker','fresh-wizard','start-owner','start-admin','watch','verify'])
    ap.add_argument('--authorize-reset', action='store_true')
    args=ap.parse_args()
    if os.geteuid()!=0 or output(['hostname']).strip()!='demo' or not OWNER.is_dir():
        raise RuntimeError('This runner is bound to root@demo and the disposable dana fixture')
    if output(['systemctl','is-active','tailscaled']).strip()!='active':
        raise RuntimeError('Host access prerequisite failed')
    P.mkdir(mode=0o700, exist_ok=True)
    os.chmod(P, 0o700)
    if args.phase=='baseline':
        if (P/'baseline.json').exists(): raise RuntimeError('Do not overwrite initial baseline')
        containers=json.loads(output(['docker','inspect']+output(['docker','ps','-aq']).split()))
        inventory=[{'id':x['Id'], 'name':x['Name'], 'image':x['Config']['Image'], 'workdir':x['Config'].get('Labels',{}).get('com.docker.compose.project.working_dir')} for x in containers]
        if any(x['workdir']!='/home/dana/runtipi' and not str(x['workdir']).startswith('/data/apps/wizardapps/') for x in inventory):
            raise RuntimeError('Unrelated Docker containers: refuse host teardown')
        receipt('baseline', {'hashes':snapshot(), 'owner_env_sha256':owner_env_hash(), 'containers':inventory, 'sudo_password_required':command(['runuser','-u','dana','--','sudo','-n','true'],'baseline-sudo',check=False)!=0})
        backup_rc=command(['tar','--warning=no-file-changed','-cpf',str(P/'dana-before.tar'),'-C',str(OWNER),'.hermes','.wizard-os','runtipi','.config','wizard-test-baseline','notes'],'backup',check=False)
        if backup_rc not in (0,1): raise RuntimeError('Backup failed; no destructive phase is allowed')
        command(['tar','-tf',str(P/'dana-before.tar')],'backup-list')
        (P/'backup-verified').write_text('Tar readable; mutable runtime files may change during snapshot.\n')
        command(['tailscale','serve','status','--json'],'serve-before')
    elif args.phase=='no-docker':
        if not args.authorize_reset or not (P/'backup-verified').is_file(): raise RuntimeError('Verified backup and reset authorization required')
        baseline=json.loads((P/'baseline.json').read_text())
        ids=output(['docker','ps','-aq','--no-trunc']).split()
        if set(ids)!={x['id'] for x in baseline['containers']}: raise RuntimeError('Container inventory changed')
        command(['systemctl','--user','--machine=dana@','stop','wizard-app-manager-api.service','wizard-store-data.service'],'stop-owner')
        command(['docker','stop','--time','45']+ids,'stop-containers')
        command(['docker','rm']+ids,'remove-owned-containers')
        # Exact currently installed Wizard core images only; no global prune or unrelated images.
        core=sorted({x['image'] for x in baseline['containers'] if any(x['image'].startswith('ghcr.io/humanitylabs-org/'+n+'@') for n in ['wizard-ux','wizard-agent-service','wizard-app-manager'])})
        command(['docker','image','rm']+core,'remove-core-images')
        receipt('core-image-cache-removed', {'references':core, 'global_prune':False})
        command(['systemctl','stop','docker.service','docker.socket','containerd.service'],'stop-docker')
        command(['apt-get','remove','-y','docker-ce','docker-ce-cli','containerd.io','docker-buildx-plugin','docker-compose-plugin','docker-ce-rootless-extras'],'remove-docker',timeout=300)
        # Preserve volumes/images, user configuration, Tailscale and SSH. No prune/purge.
        missing=command(['sh','-c','command -v docker'],'docker-absent',check=False)!=0
        receipt('no-docker', {'docker_binary_absent':missing,'tailscaled_active':output(['systemctl','is-active','tailscaled']).strip(), 'fixture_hashes_unchanged':snapshot()==baseline['hashes']})
        if not missing: raise RuntimeError('Docker binary still exists')
    elif args.phase=='fresh-wizard':
        if not args.authorize_reset or not (P/'no-docker.json').is_file(): raise RuntimeError('Teardown prerequisite required')
        linked=OWNER/'.wizard-os/linked.json'
        if linked.exists() and json.loads(linked.read_text()).get('state')!='unlinked':
            raise RuntimeError('Run the supported wizard unlink before fresh reset; do not orphan a managed plugin')
        for name in ['runtipi','.wizard-os']:
            src=OWNER/name; dest=P/('archived-'+name)
            if dest.exists(): raise RuntimeError('Fresh-state archive already exists')
            src.rename(dest)
        receipt('fresh-wizard', {'wizard_runtime_archived':True,'hermes_untouched':snapshot()==json.loads((P/'baseline.json').read_text())['hashes'], 'limitations':['Hermes fixture previously linked, now officially unlinked; host Tailscale stays joined; non-core image/layer caches retained']})
    elif args.phase=='start-owner':
        command(['curl','-fsSL','https://aiwizards.com/os.sh','-o',str(P/'official-get.sh')],'download')
        command(['install','-m','644',str(P/'official-get.sh'),'/home/dana/acceptance-get.sh'],'owner-script')
        rc=command(['runuser','-u','dana','--','env','HOME=/home/dana','USER=dana','XDG_RUNTIME_DIR=/run/user/1000','_HERMES_GATEWAY=1','HERMES_SESSION_PLATFORM=telegram','bash','/home/dana/acceptance-get.sh','start'],'owner-start',check=False)
        text=(P/'owner-start.log').read_text()
        expected='curl -fsSL https://aiwizards.com/os.sh | sudo env WIZARD_HERMES_USER=dana HERMES_HOME=/home/dana/.hermes WIZARD_AGENT_SAFE=1 bash -s start'
        receipt('owner-admin-gate', {'exit_code':rc,'exact_admin_command_printed':expected in text, 'actual_password_entry_exercised':False})
        if rc!=2 or expected not in text: raise RuntimeError('Owner admin gate did not match expected command')
    elif args.phase=='start-admin':
        # Exact printed command with root executing sudo env; no sudo password is entered.
        command(['bash','-c','set -o pipefail; curl -fsSL https://aiwizards.com/os.sh | sudo env WIZARD_HERMES_USER=dana HERMES_HOME=/home/dana/.hermes WIZARD_AGENT_SAFE=1 bash -s start'],'admin-start')
        receipt('admin-start', {'started':True,'actual_password_entry_exercised':False})
    elif args.phase=='watch':
        deadline=time.monotonic()+1800
        while time.monotonic()<deadline:
            f=OWNER/'.wizard-os/install-status.json'
            if f.exists():
                st=json.loads(f.read_text())
                if st.get('state') in ('done','failed'):
                    tag=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                    st['evidence_log']='official-install-'+tag+'.log'
                    command(['cp',str(OWNER/'.wizard-os/install.log'),str(P/st['evidence_log'])],'save-install-log')
                    command(['cp',str(P/st['evidence_log']),str(P/'official-install.log')],'save-install-log')
                    receipt('install-finish',st)
                    if st['state']!='done': raise RuntimeError('Official installer failed')
                    return
            time.sleep(5)
        raise TimeoutError('Installer exceeded 30 minute bound')
    elif args.phase=='verify':
        baseline=json.loads((P/'baseline.json').read_text()); before=baseline['hashes']; after=snapshot()
        changes=[k for k in before if before[k]!=after.get(k)]
        wizard_env_only=(changes==['.hermes/.env'] and baseline.get('owner_env_sha256')==owner_env_hash())
        unexpected_changes=[] if wizard_env_only else changes
        report=json.loads((OWNER/'.wizard-os/release.json').read_text())
        command(['docker','ps','--format','{{.Names}} {{.Image}} {{.Status}}'],'final-containers')
        command(['tailscale','serve','status'],'serve-after')
        deadline=time.monotonic()+180
        while command(['runuser','-l','dana','-c','XDG_RUNTIME_DIR=/run/user/1000 /home/dana/.local/bin/wizard doctor'],'doctor',timeout=45,check=False):
            if time.monotonic()>deadline: raise RuntimeError('Doctor did not recover after deferred restart')
            time.sleep(5)
        no_sudo=command(['runuser','-u','dana','--','sudo','-n','true'],'final-sudo',check=False)!=0
        receipt('verify', {'preserved_exactly':not changes,'known_link_env_change_only':wizard_env_only,'owner_data_preserved':not unexpected_changes,'changed_paths':changes,'sudo_password_required':no_sudo,'release':report['release'],'release_status':report['status'],'all_release_checks_pass':all(x['ok'] for x in report['checks']),'tailscaled_active':output(['systemctl','is-active','tailscaled']).strip()})
        if unexpected_changes or not no_sudo or report['status']!='pinned' or not all(x['ok'] for x in report['checks']): raise RuntimeError('Final verification failed')

if __name__=='__main__': main()
