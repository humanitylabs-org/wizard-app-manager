import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { scrubLeakedManagerCredentials } from '../leaked-env-repair';

// Synthetic sentinels only.
const DB = 'sentinel-db-password-000111';
const QUEUE = 'sentinel-queue-password-222333';
const JWT = 'sentinel-jwt-secret-444555666';
const dirs: string[] = [];
afterEach(() => dirs.splice(0).forEach((d) => fs.rmSync(d, { recursive: true, force: true })));

describe('upgrade repair of leaked manager credentials', () => {
  it('removes only exact manager credential lines and keeps app data intact', async () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'wizard-scrub-'));
    dirs.push(root);
    const app = path.join(root, 'wizardapps', 'demo');
    fs.mkdirSync(path.join(app, 'data'), { recursive: true });
    const env = ['TZ=UTC', `POSTGRES_PASSWORD=${DB}`, 'APP_SECRET=app-own-generated-secret-xyz', `JWT_SECRET="${JWT}"`, `RABBITMQ_PASSWORD=${QUEUE}`, ''].join('\n');
    fs.writeFileSync(path.join(app, 'app.env'), env, { mode: 0o666 });
    fs.writeFileSync(path.join(app, 'data', 'db.conf'), `password=${DB}\n`); // app-owned: never touched
    const outside = path.join(root, 'outside.env');
    fs.writeFileSync(outside, `X=${DB}\n`);
    fs.mkdirSync(path.join(root, 'wizardapps', 'linked'));
    fs.symlinkSync(outside, path.join(root, 'wizardapps', 'linked', 'app.env'));

    const repaired = await scrubLeakedManagerCredentials(root, [DB, QUEUE, JWT, undefined, 'short']);
    expect(repaired).toEqual(['wizardapps/demo']);
    const after = fs.readFileSync(path.join(app, 'app.env'), 'utf8');
    expect(after).toBe('TZ=UTC\nAPP_SECRET=app-own-generated-secret-xyz\n');
    for (const s of [DB, QUEUE, JWT]) expect(after).not.toContain(s);
    expect(fs.statSync(path.join(app, 'app.env')).mode & 0o777).toBe(0o600);
    expect(fs.readFileSync(path.join(app, 'data', 'db.conf'), 'utf8')).toBe(`password=${DB}\n`);
    expect(fs.readFileSync(outside, 'utf8')).toBe(`X=${DB}\n`); // planted link not followed
    expect(await scrubLeakedManagerCredentials(root, [DB, QUEUE, JWT])).toEqual([]); // idempotent
  });
});
