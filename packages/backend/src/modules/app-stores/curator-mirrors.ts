import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const exec = promisify(execFile);
export const SOURCE_MIRROR = 'https://github.com/humanitylabs-org/wizard-app-manager/releases/download/curator-source-mirrors';
export const BUNDLE_LIMIT_BYTES = 256 * 1024 * 1024;
export const IMAGE_MIRROR = 'ghcr.io/humanitylabs-org/wizard-app-manager';
export const sourceRef = (repo: string, commit: string) => {
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) || !/^[0-9a-f]{40}$/.test(commit))
    throw new Error('mirror requires a repository and exact 40-hex commit');
  return `refs/tags/curator/${repo}/${commit}`;
};

export const sourceName = (repo: string, commit: string) => {
  sourceRef(repo, commit);
  return `${repo.replace('/', '--')}--${commit}.bundle`;
};
const downloadBundle = async (url: string, dest: string) => {
  const response = await fetch(url, { signal: AbortSignal.timeout(90000) });
  if (!response.ok || !response.body) throw new Error('curator bundle unavailable');
  const out = await fs.open(dest, 'w');
  const reader = response.body.getReader();
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > BUNDLE_LIMIT_BYTES) throw new Error('curator bundle exceeds size limit');
      await out.writeFile(value);
    }
  } finally { reader.releaseLock(); await out.close(); }
};

// Full git object verification, not an archive filename/header claiming a SHA.
// A fetch outage permits fallback; an integrity mismatch never does.
export const fetchPinnedCommit = async (url: string, commit: string, dest: string, mirror = SOURCE_MIRROR) => {
  if (!/^https:\/\//.test(url)) throw new Error('refusing non-https source');
  if (!/^[0-9a-f]{40}$/.test(commit)) throw new Error('expected exact 40-hex commit');
  const repo = /^https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+?)(?:\.git)?\/?$/.exec(url)?.[1];
  const git = async (...args: string[]) => (await exec('git', ['-c', 'advice.detachedHead=false', '-c', `safe.directory=${dest}`, ...args],
    { cwd: dest, timeout: 180000, env: { ...process.env, GIT_TERMINAL_PROMPT: '0' } })).stdout.trim();
  const init = async () => {
    await fs.rm(dest, { recursive: true, force: true });
    await fs.mkdir(dest, { recursive: true });
    await git('init', '-q');
  };
  await init();
  let used = url;
  try { await git('fetch', '-q', '--depth', '1', url, commit); }
  catch (originError) {
    if (!repo) throw originError;
    await init();
    const bundle = path.join(dest, '.git', 'curator.bundle');
    const mirrorUrl = `${mirror}/${sourceName(repo, commit)}`;
    try {
      await downloadBundle(mirrorUrl, bundle);
      await git('fetch', '-q', bundle, sourceRef(repo, commit));
    } finally { await fs.rm(bundle, { force: true }); }
    used = mirrorUrl;
  }
  await git('checkout', '-q', 'FETCH_HEAD');
  const head = await git('rev-parse', 'HEAD');
  if (head !== commit) {
    await fs.rm(dest, { recursive: true, force: true });
    throw new Error(`fetched ${head} instead of ${commit}`);
  }
  return used;
};

export const imageRef = (image: string) => {
  if (image.startsWith('ghcr.io/humanitylabs-org/')) return undefined;
  const digest = /^[^\s@$]+@sha256:([0-9a-f]{64})$/.exec(image)?.[1];
  return digest ? `${IMAGE_MIRROR}:curator-sha256-${digest}@sha256:${digest}` : undefined;
};
export const resolveImages = async (compose: { services?: Record<string, Record<string, unknown>> }, probe = async (image: string) => {
  try { await exec('docker', ['image', 'inspect', image], { timeout: 15000 }); return true; } catch { /* registry-only below */ }
  try { await exec('docker', ['manifest', 'inspect', image], { timeout: 45000 }); return true; } catch { return false; }
}) => {
  for (const service of Object.values(compose.services ?? {})) {
    const image = typeof service.image === 'string' ? service.image.replace(/\$\{[A-Z_][A-Z_0-9]*:-([^}]+)\}/g, '$1') : service.image;
    const mirror = typeof image === 'string' && !service.build ? imageRef(image) : undefined;
    if (mirror && !(await probe(image as string)) && await probe(mirror))
      service.image = (service.image as string).replace(image as string, mirror);
  }
};
