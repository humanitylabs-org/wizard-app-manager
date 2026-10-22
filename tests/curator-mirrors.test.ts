import { test, expect } from 'bun:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fetchPinnedCommit, sourceRef, sourceName, imageRef, resolveImages } from '../packages/backend/src/modules/app-stores/curator-mirrors';

test('real git: missing origin -> mirror -> exact SHA; wrong SHA rejected; origin wins', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'curator-'));
  const original = process.env.GIT_CONFIG_GLOBAL;
  const originalFetch = globalThis.fetch;
  // Exercise Bun 1.3.0's optimized-reader shape even when this host selects
  // the standard reader. Bytes still come from the real local HTTP server.
  globalThis.fetch = (async (...args: Parameters<typeof fetch>) => {
    const response = await originalFetch(...args);
    if (response.body) {
      const reader = response.body.getReader();
      Object.defineProperty(response, 'body', { value: { getReader: () => ({ read: reader.read.bind(reader) }) } });
    }
    return response;
  }) as typeof fetch;
  const assets = path.join(root, 'assets');
  await fs.mkdir(assets);
  const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: request => {
    const file = Bun.file(path.join(assets, path.basename(new URL(request.url).pathname)));
    return new Response(file);
  } });
  const base = `http://127.0.0.1:${server.port}`;
  const git = (cwd: string, ...args: string[]) => execFileSync('git', args, { cwd, encoding: 'utf8' }).trim();
  try {
    const src = path.join(root, 'builder');
    const mirror = path.join(root, 'mirror.git');
    const dest = path.join(root, 'checkout');
    await fs.mkdir(src);
    git(src, 'init', '-q');
    git(src, 'config', 'user.name', 'Test');
    git(src, 'config', 'user.email', 'test@example.org');
    await fs.writeFile(path.join(src, 'server.py'), 'pinned code');
    git(src, 'add', '.'); git(src, 'commit', '-qm', 'source');
    const sha = git(src, 'rev-parse', 'HEAD');
    git(root, 'clone', '-q', '--bare', src, mirror);
    git(mirror, 'update-ref', sourceRef('gone/builder', sha), sha);
    git(mirror, 'bundle', 'create', path.join(assets, sourceName('gone/builder', sha)), sourceRef('gone/builder', sha));
    const config = path.join(root, 'gitconfig');
    await fs.writeFile(config, `[url "file://${root}/unreachable"]\n insteadOf = https://github.com/gone/builder\n`);
    process.env.GIT_CONFIG_GLOBAL = config;
    expect(await fetchPinnedCommit('https://github.com/gone/builder.git', sha, dest, base)).toBe(`${base}/${sourceName('gone/builder', sha)}`);
    expect(git(dest, 'rev-parse', 'HEAD')).toBe(sha);
    expect(await fs.readFile(path.join(dest, 'server.py'), 'utf8')).toBe('pinned code');
    const wrong = 'b'.repeat(40);
    git(mirror, 'update-ref', sourceRef('gone/builder', wrong), sha);
    git(mirror, 'bundle', 'create', path.join(assets, sourceName('gone/builder', wrong)), sourceRef('gone/builder', wrong));
    await expect(fetchPinnedCommit('https://github.com/gone/builder', wrong, dest, base)).rejects.toThrow('instead of');
    expect(await fs.access(dest).then(() => true, () => false)).toBe(false);
    await fs.writeFile(config, `[url "file://${src}"]\n insteadOf = https://github.com/gone/builder\n`);
    expect(await fetchPinnedCommit('https://github.com/gone/builder', sha, dest, '/missing-mirror')).toBe('https://github.com/gone/builder');
    await expect(fetchPinnedCommit('https://github.com/gone/builder', 'main', dest)).rejects.toThrow('40-hex');
  } finally {
    server.stop(true);
    globalThis.fetch = originalFetch;
    if (original === undefined) delete process.env.GIT_CONFIG_GLOBAL; else process.env.GIT_CONFIG_GLOBAL = original;
    await fs.rm(root, { recursive: true, force: true });
  }
});

test('image fallback is origin-first and keeps digest, never tags', async () => {
  const image = `python:3@sha256:${'a'.repeat(64)}`;
  const mirror = imageRef(image)!;
  const compose = { services: { app: { image }, mutable: { image: 'python:latest' } } };
  const calls: string[] = [];
  await resolveImages(compose, async ref => { calls.push(ref); return ref === mirror; });
  expect(calls).toEqual([image, mirror]);
  expect(compose.services.app.image).toBe(mirror);
  expect(mirror.endsWith(`@sha256:${'a'.repeat(64)}`)).toBe(true);
  compose.services.app.image = image;
  calls.length = 0;
  await resolveImages(compose, async ref => { calls.push(ref); return true; });
  expect(calls).toEqual([image]);
  expect(compose.services.app.image).toBe(image);
  compose.services.app.image = '${OWNER_IMAGE:-' + image + '}';
  await resolveImages(compose, async ref => ref === mirror);
  expect(compose.services.app.image).toBe('${OWNER_IMAGE:-' + mirror + '}');
});
