import { test, expect, spyOn } from 'bun:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { WizardStorage } from '../packages/backend/src/modules/docker/wizard-storage';

for (const fail of [false, true]) test(`factory ownership hook ${fail ? 'blocks failed repair' : 'completes before compose start'}`, async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'factory-owner-'));
  const app = path.join(root, 'apps/wizardapps/wizard-recorder');
  const folder = path.join(root, 'app-data/wizardapps/wizard-recorder/data');
  await fs.mkdir(app, { recursive: true }); await fs.mkdir(folder, { recursive: true });
  await fs.writeFile(path.join(app, 'wizard.md'), '---\nid: wizard-recorder\n---\n');
  const compose = JSON.stringify({ services: { recorder: { user: '1000:1000', 'x-runtipi': { is_main: true }, volumes: ['${APP_DATA_DIR}/data:/data'] } } });
  await fs.writeFile(path.join(app, 'docker-compose.yml'), compose);
  const events: string[] = [];
  const original = fs.lstat.bind(fs);
  const stat = spyOn(fs, 'lstat').mockImplementation(async (...args: any[]) => {
    const s = await (original as any)(...args);
    return args[0] === folder ? Object.assign(Object.create(s), { uid: 0, gid: 0 }) : s;
  });
  const owner = spyOn(fs, 'chown').mockImplementation(async (p, uid, gid) => {
    expect([p, uid, gid]).toEqual([folder, 1000, 1000]); events.push('chown');
    if (fail) throw new Error('ownership denied');
  });
  try {
    const storage = new WizardStorage({ dataDir: root, appDataDir: path.join(root, 'app-data') }, async () => [{ appName: 'wizard-recorder', appStoreSlug: 'wizardapps' }]);
    const result = storage.prepare('wizard-recorder:wizardapps', compose, async () => { events.push('start'); });
    if (fail) { await expect(result).rejects.toThrow('ownership denied'); expect(events).toEqual(['chown']); }
    else { await result; expect(events).toEqual(['chown', 'start']); }
  } finally { stat.mockRestore(); owner.mockRestore(); await fs.rm(root, { recursive: true, force: true }); }
});
