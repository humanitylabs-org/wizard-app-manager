// Run only on an explicitly selected throwaway Docker daemon/context.
// This invokes production migrate(), checks uid/gid/mode + dotfiles + links and
// idempotence, then removes only the randomly named QA objects it created.
import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { WizardStorage, COPY_IMAGE } from '../packages/backend/src/modules/docker/wizard-storage';
const exec = promisify(execFile);
if (process.env.WIZARD_STORAGE_DOCKER_PROOF !== '1') throw new Error('Set WIZARD_STORAGE_DOCKER_PROOF=1 on throwaway proof');
const root = await fs.mkdtemp(path.join(process.env.TMPDIR!, 'wizard-copy-proof-'));
const volume = `wizard-storage-qa-${Date.now()}`;
// Linux-side bind storage permits real Unix owners (macOS virtiofs cannot).
const hostData = `/var/lib/wizard-storage-qa/${volume}`;
const run = async (args: string[]) => (await exec('docker', args)).stdout;
const helper = async (script: string, old = true) =>
  run([
    'run',
    '--rm',
    '--network',
    'none',
    ...(old ? ['--mount', `type=volume,src=${volume},dst=/old`] : []),
    '--mount',
    `type=bind,src=${hostData},dst=/state`,
    COPY_IMAGE,
    'sh',
    '-ec',
    script,
  ]);
try {
  await run(['volume', 'create', volume]);
  await run([
    'run',
    '--rm',
    '--network',
    'none',
    '--mount',
    'type=bind,src=/var/lib,dst=/host',
    COPY_IMAGE,
    'mkdir',
    '-p',
    `/host/wizard-storage-qa/${volume}/volumes`,
  ]);
  await helper(
    'mkdir /old/nested; printf proof-only > /old/nested/.login; chmod 600 /old/nested/.login; chown -R 1234:2345 /old; chmod 700 /old/nested; ln -s nested/.login /old/link',
  );
  const storage = new WizardStorage({ dataDir: root, appDataDir: root });
  const app = { urn: 'proof:wizardapps', manifest: { id: 'proof' }, compose: { services: {} }, data: root, hostData };
  await storage.migrate(volume, 'volumes/login', app);
  const first = await helper(
    'stat -c "%u:%g:%a" /state/volumes/login/nested/.login; stat -c "%u:%g:%a" /state/volumes/login/nested; cat /state/volumes/login/nested/.login; readlink /state/volumes/login/link; test -f /state/volumes/.login.wizard-migrated',
  );
  if (!first.includes('1234:2345:600') || !first.includes('1234:2345:700') || !first.includes('proof-only') || !first.includes('nested/.login'))
    throw new Error(`Attributes not preserved: ${first}`);
  const archived = await helper('tar -czpf /state/app-state.tar.gz -C /state volumes; tar -xzOf /state/app-state.tar.gz volumes/login/nested/.login');
  if (archived !== 'proof-only') throw new Error('Archive omitted migrated volume data');
  await helper('printf changed-after-copy > /state/volumes/login/nested/.login');
  await storage.migrate(volume, 'volumes/login', app);
  const second = await helper('cat /state/volumes/login/nested/.login; cat /old/nested/.login');
  if (second !== 'changed-after-copyproof-only') throw new Error(`Copy-once violated: ${second}`);
  await helper('mkdir -p /state/volumes/nonempty; printf keep > /state/volumes/nonempty/existing');
  let refused = false;
  try {
    await storage.migrate(volume, 'volumes/nonempty', app);
  } catch {
    refused = true;
  }
  if (!refused) throw new Error('Nonempty destination was not refused');
  // An interrupted staging directory is retried, not adopted as an empty login.
  await helper('mkdir /state/volumes/.retry.wizard-copy; printf partial > /state/volumes/.retry.wizard-copy/partial');
  await storage.migrate(volume, 'volumes/retry', app);
  const retry = await helper('cat /state/volumes/retry/nested/.login; test ! -e /state/volumes/retry/partial');
  if (retry !== 'proof-only') throw new Error('Interrupted copy retry failed');
  const retained = (await run(['volume', 'inspect', volume, '--format', '{{.Name}}'])).trim();
  const checks = {
    file_uid_gid_mode: first.includes('1234:2345:600'),
    directory_uid_gid_mode: first.includes('1234:2345:700'),
    dotfile: first.includes('proof-only'),
    symlink: first.includes('nested/.login'),
    copy_once: second === 'changed-after-copyproof-only',
    nonempty_refused: refused,
    interrupted_retry: retry === 'proof-only',
    backup_bytes: archived === 'proof-only',
    old_volume_retained: retained === volume,
  };
  if (Object.values(checks).some((v) => !v)) throw new Error('One or more proof checks failed');
} finally {
  await run(['volume', 'rm', volume]);
  await helper('rm -rf /state/volumes /state/app-state.tar.gz', false);
  await run([
    'run',
    '--rm',
    '--network',
    'none',
    '--mount',
    'type=bind,src=/var/lib/wizard-storage-qa,dst=/qa',
    COPY_IMAGE,
    'rmdir',
    `/qa/${volume}`,
  ]);
  await fs.rm(root, { recursive: true, force: true });
}
