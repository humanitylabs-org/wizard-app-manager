import fs from 'node:fs';
import path from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';
import { copySource, preparePackage, pruneSources, repoPath, sourceDir } from '../packaging';

const sha = 'b'.repeat(40);

describe('runtime.package (source | build | image)', () => {
  let tmp: string;
  let src: string;
  let target: string;
  beforeEach(async () => {
    tmp = path.join('/tmp', `pkg-${Math.random().toString(36).slice(2)}`); // fs is memfs in this suite
    await fs.promises.mkdir(tmp, { recursive: true });
    src = path.join(tmp, 'checkout');
    await fs.promises.mkdir(path.join(src, '.git'), { recursive: true });
    await fs.promises.mkdir(path.join(src, 'app'), { recursive: true });
    await fs.promises.writeFile(path.join(src, '.git', 'HEAD'), 'ref');
    await fs.promises.writeFile(path.join(src, 'app', 'server.py'), 'print(1)');
    await fs.promises.writeFile(path.join(src, 'app', 'Dockerfile'), 'FROM x');
    target = sourceDir(path.join(tmp, 'app-data'), 'wizardapps', 'demo', sha);
  });

  it('keeps the source dir under app-data, keyed by commit', () => {
    expect(target).toBe(path.join(tmp, 'app-data', '.wizard-source', 'wizardapps', `demo@${sha.slice(0, 12)}`));
  });

  it('legacy apps (no package) and image apps copy nothing', async () => {
    const compose = { services: { demo: { image: 'x@sha256:1' } } };
    expect(await preparePackage({ pkg: undefined, compose, src, target, appName: 'demo', commit: sha })).toBe(false);
    expect(await preparePackage({ pkg: 'image', compose, src, target, appName: 'demo', commit: sha })).toBe(false);
    expect(fs.existsSync(target)).toBe(false);
  });

  it('source apps get the pinned code without .git', async () => {
    const compose = { services: { demo: { image: 'python@sha256:1', volumes: ['${APP_SOURCE_DIR}/app:/app:ro'] } } };
    expect(await preparePackage({ pkg: 'source', compose, src, target, appName: 'demo', commit: sha })).toBe(true);
    expect(fs.readFileSync(path.join(target, 'app', 'server.py'), 'utf8')).toBe('print(1)');
    expect(fs.existsSync(path.join(target, '.git'))).toBe(false);
    expect(compose.services.demo.image).toBe('python@sha256:1');
  });

  it('build apps build from the copy under a commit-named local image', async () => {
    const compose: { services: Record<string, Record<string, unknown>> } = { services: { web: { build: { context: 'app' } } } };
    await preparePackage({ pkg: 'build', compose, src, target, appName: 'demo', commit: sha });
    expect(compose.services.web).toMatchObject({
      build: { context: path.join(target, 'app') },
      image: `wizard-local/demo-web:${sha.slice(0, 12)}`,
      pull_policy: 'build',
    });
  });

  it('rejects the wrong shape for each package type', async () => {
    const built = { services: { demo: { image: 'x', build: '.' } } };
    await expect(preparePackage({ pkg: 'source', compose: built, src, target, appName: 'demo', commit: sha })).rejects.toThrow(/remove build:/);
    await expect(preparePackage({ pkg: 'image', compose: built, src, target, appName: 'demo', commit: sha })).rejects.toThrow(/remove build:/);
    await expect(
      preparePackage({ pkg: 'build', compose: { services: { demo: { image: 'x' } } }, src, target, appName: 'demo', commit: sha }),
    ).rejects.toThrow(/need a build/);
    await expect(preparePackage({ pkg: 'docker', compose: built, src, target, appName: 'demo', commit: sha })).rejects.toThrow(/source, build or image/);
    for (const context of ['/etc', '..', '${APP_SOURCE_DIR}']) {
      await expect(
        preparePackage({ pkg: 'build', compose: { services: { demo: { build: { context } } } }, src, target, appName: 'demo', commit: sha }),
      ).rejects.toThrow(/inside the repo/);
    }
    await expect(
      preparePackage({ pkg: 'build', compose: { services: { demo: { build: { context: 'app', dockerfile: '../../x' } } } }, src, target, appName: 'demo', commit: sha }),
    ).rejects.toThrow(/inside the context/);
  });

  it('refuses symlinks that leave the app and sources over the size cap', async () => {
    await fs.promises.symlink('/etc/passwd', path.join(src, 'app', 'leak'));
    await expect(copySource(src, path.join(tmp, 'out'))).rejects.toThrow(/outside the app/);
    await fs.promises.rm(path.join(src, 'app', 'leak'));
    await fs.promises.symlink('server.py', path.join(src, 'app', 'alias.py'));
    await copySource(src, path.join(tmp, 'ok'));
    expect(fs.readlinkSync(path.join(tmp, 'ok', 'app', 'alias.py'))).toBe('server.py');
    await expect(copySource(src, path.join(tmp, 'big'), 4)).rejects.toThrow(/larger than/);
  });

  it('prunes older commits of the same app only', async () => {
    const root = path.join(tmp, 'app-data');
    for (const n of ['demo@aaaaaaaaaaaa', `demo@${sha.slice(0, 12)}`, 'other@aaaaaaaaaaaa']) {
      await fs.promises.mkdir(path.join(root, '.wizard-source', 'wizardapps', n), { recursive: true });
    }
    await pruneSources(root, 'wizardapps', 'demo', `demo@${sha.slice(0, 12)}`);
    expect(fs.readdirSync(path.join(root, '.wizard-source', 'wizardapps')).sort()).toEqual([`demo@${sha.slice(0, 12)}`, 'other@aaaaaaaaaaaa']);
  });

  it('repoPath normalizes and refuses escapes', () => {
    expect(repoPath('./app/../app')).toBe('app');
    expect(repoPath(undefined)).toBe('');
    expect(repoPath('../x')).toBeNull();
    expect(repoPath('/abs')).toBeNull();
  });
});
