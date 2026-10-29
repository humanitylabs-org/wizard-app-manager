import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';

// Same inventory format as wam/source_integrity.py; links are data, never followed.
export const sourceInventory = async (root: string) => {
  const stat = await fs.promises.lstat(root);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('source is not a regular directory');
  const entries: Record<string, { link: string } | { directory: true } | { sha256: string }> = {};
  let total = 0;
  const walk = async (here: string) => {
    for (const name of (await fs.promises.readdir(here)).sort()) {
      if (here === root && name === '.git') continue;
      const p = path.join(here, name), key = path.relative(root, p).split(path.sep).join('/');
      const st = await fs.promises.lstat(p);
      if (st.isSymbolicLink()) entries[key] = { link: await fs.promises.readlink(p) };
      else if (st.isDirectory()) { entries[key] = { directory: true }; await walk(p); }
      else if (st.isFile()) {
        total += st.size;
        if (total > SOURCE_LIMIT_BYTES) throw new Error('source inventory exceeds size limit');
        const hash = createHash('sha256');
        for await (const chunk of fs.createReadStream(p)) hash.update(chunk);
        entries[key] = { sha256: hash.digest('hex') };
      } else throw new Error('unsupported source file');
    }
  };
  await walk(root);
  return entries;
};
export const inventoryPath = (root: string) => `${root}.inventory.json`;
export const pristineSource = async (root: string) => {
  try {
    const expected = JSON.parse(await fs.promises.readFile(inventoryPath(root), 'utf8'));
    return isDeepStrictEqual(expected, await sourceInventory(root));
  } catch { return false; }
};
const recordSource = async (root: string) =>
  fs.promises.writeFile(inventoryPath(root), `${JSON.stringify(await sourceInventory(root))}\n`);

// spec/WIZARD_MD.md "Packaging" (DECISIONS #76): runtime.package is source | build | image.
// source/build apps get their pinned commit (without .git) copied to
// <app-data>/.wizard-source/<store>/<app>@<commit12>. The compose mounts it as ${APP_SOURCE_DIR},
// the host path the env generator sets at install. build apps get their build context pointed at
// that copy (compose reads build contexts on the App Manager side) and an image name with the commit.
export const PACKAGES = new Set(['source', 'build', 'image']);
export const SOURCE_LIMIT_BYTES = 256 * 1024 * 1024;

type Service = Record<string, unknown> & { build?: unknown; image?: unknown };

// Repo-relative path, or null when it is absolute, templated or climbs out of the repo.
export const repoPath = (p: unknown): string | null => {
  const raw = String(p ?? '.');
  if (raw.startsWith('/') || raw.includes('${')) return null;
  const out: string[] = [];
  for (const seg of raw.split('/')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') {
      if (!out.length) return null;
      out.pop();
    } else out.push(seg);
  }
  return out.join('/');
};

// Copy the fetched checkout without .git. Symlinks are kept as links but may only point inside the tree.
export const copySource = async (src: string, dst: string, limit = SOURCE_LIMIT_BYTES) => {
  const root = path.resolve(src);
  let total = 0;
  const walk = async (from: string, to: string) => {
    await fs.promises.mkdir(to, { recursive: true });
    for (const name of await fs.promises.readdir(from)) {
      if (from === root && name === '.git') continue;
      const a = path.join(from, String(name));
      const b = path.join(to, String(name));
      const e = await fs.promises.lstat(a);
      if (e.isSymbolicLink()) {
        const target = await fs.promises.readlink(a);
        const resolved = path.resolve(path.dirname(a), target);
        if (path.isAbsolute(target) || (resolved !== root && !resolved.startsWith(`${root}${path.sep}`)))
          throw new Error(`source symlink ${path.relative(root, a)} points outside the app`);
        await fs.promises.symlink(target, b);
      } else if (e.isDirectory()) {
        await walk(a, b);
      } else if (e.isFile()) {
        total += e.size;
        if (total > limit) throw new Error(`app source is larger than ${Math.round(limit / 1048576)} MB`);
        await fs.promises.copyFile(a, b);
      }
    }
  };
  await walk(root, dst);
};

export const SOURCE_ROOT = '.wizard-source';
export const sourceDirName = (appName: string, commit: string) => `${appName}@${commit.slice(0, 12)}`;
export const sourceDir = (appDataRoot: string, storeId: string, appName: string, commit: string) =>
  path.join(appDataRoot, SOURCE_ROOT, storeId, sourceDirName(appName, commit));

// Keep only the source copy for the commit being installed; older copies go once their app is recreated.
export const pruneSources = async (appDataRoot: string, storeId: string, appName: string, keep: string) => {
  const dir = path.join(appDataRoot, SOURCE_ROOT, storeId);
  const names = await fs.promises.readdir(dir).catch(() => [] as string[]);
  for (const n of names) {
    if (n.startsWith(`${appName}@`) && n !== keep && !n.includes('/') && await pristineSource(path.join(dir, n))) {
      await fs.promises.rm(path.join(dir, n), { recursive: true, force: true });
      await fs.promises.rm(inventoryPath(path.join(dir, n)), { force: true });
    }
  }
};

export const preparePackage = async (opts: {
  pkg: unknown;
  compose: { services?: Record<string, Service> };
  src: string; // fetched checkout at the pinned commit
  target: string; // sourceDir(...) for this app and commit
  appName: string;
  commit: string;
}) => {
  const { pkg, compose, src, target, appName, commit } = opts;
  if (pkg === undefined || pkg === null) return false; // legacy app: installs as before
  const kind = String(pkg);
  if (!PACKAGES.has(kind)) throw new Error(`runtime.package must be source, build or image, not ${kind}`);
  const services = Object.entries(compose?.services ?? {}).filter(([, s]) => s && typeof s === 'object');
  const builds = services.filter(([, s]) => s.build);
  if (kind !== 'build' && builds.length) throw new Error(`${kind} apps build nothing; remove build: from ${builds.map(([n]) => n).join(', ')}`);
  if (kind === 'build' && !builds.length) throw new Error('build apps need a build: section');
  if (kind === 'image') return false;
  for (const [name, s] of builds) {
    const b: Record<string, unknown> = typeof s.build === 'string' ? { context: s.build } : { ...(s.build as Record<string, unknown>) };
    const ctx = repoPath(b.context);
    if (ctx === null) throw new Error(`${name}: build.context must be a path inside the repo`);
    const df = repoPath(path.posix.join(ctx || '.', String(b.dockerfile ?? 'Dockerfile')));
    if (df === null || (ctx && !df.startsWith(`${ctx}/`))) throw new Error(`${name}: build.dockerfile must stay inside the context`);
    b.context = ctx ? path.join(target, ctx) : target;
    s.build = b;
    s.image = `wizard-local/${appName}-${name}:${commit.slice(0, 12)}`;
    s.pull_policy = 'build';
  }
  const exists = await fs.promises.lstat(target).then(() => true, (e: NodeJS.ErrnoException) => {
    if (e.code === 'ENOENT') return false;
    throw e;
  });
  if (exists) {
    if (!isDeepStrictEqual(await sourceInventory(target), await sourceInventory(src)))
      throw new Error('Owner source differs: keep or merge your changes before updating; nothing replaced');
    await recordSource(target);
    return true;
  }
  const tmp = `${target}.tmp-${process.pid}`;
  await fs.promises.rm(tmp, { recursive: true, force: true });
  await copySource(src, tmp);
  // Never remove a destination which appeared while copying.
  await fs.promises.rename(tmp, target);
  await recordSource(target);
  return true;
};
