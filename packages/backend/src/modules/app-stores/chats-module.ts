// Keep admission in sync with wam/chats_views.py; this is reviewed trusted code, not a sandbox.
import { createHash } from 'node:crypto';
import { mkdir, readFile, realpath, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';

export async function prepareChatsModule(source: string, destination: string, manifest: Record<string, unknown>, commit: string) {
  const view = manifest.chats_view as { contract?: string; module?: string } | undefined;
  const provides = Array.isArray(manifest.provides) ? manifest.provides : [];
  if (view === undefined && !provides.includes('chats')) return;
  if (!view || typeof view !== 'object' || Object.keys(view).sort().join(',') !== 'contract,module' ||
      manifest.id === 'wizard-chats' || view.contract !== 'wizard.chats-view/1' || !provides.includes('chats') ||
      typeof view.module !== 'string' || !/^[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*\.js$/.test(view.module) ||
      !/^[a-f0-9]{40}$/.test(commit)) throw Error('Invalid Chats module declaration');
  const file = path.resolve(await realpath(source), view.module);
  if (await realpath(file) !== file || !(await stat(file)).isFile()) throw Error('Chats module symlinks are forbidden');
  if ((await stat(file)).size > 128 * 1024) throw Error('Chats module exceeds 128 KiB');
  const bytes = await readFile(file);
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  if (!bytes.length || bytes.length > 128 * 1024 || /\b(?:addEventListener|setTimeout|setInterval|requestAnimationFrame|fetch|XMLHttpRequest|__wos|__wux|localStorage|sessionStorage|innerHTML|outerHTML|history|eval|Function|WebSocket|import)\b|document\s*\.\s*cookie/.test(text))
    throw Error('Chats module uses a banned API or exceeds 128 KiB');
  const target = path.join(destination, view.module);
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, bytes);
  await writeFile(path.join(destination, '.wizard-chats-view.json'), JSON.stringify({
    contract: 'wizard.chats-view/1', module: view.module, sha256: createHash('sha256').update(bytes).digest('hex'), bytes: bytes.length, commit,
  }) + '\n');
}
