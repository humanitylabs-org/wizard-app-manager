import fs from 'node:fs/promises';
import path from 'node:path';

/** Upgrade repair for boxes that ran a build whose app.env inherited the manager's own environment.
 *
 * Removes ONLY lines whose value is exactly one of this manager's credentials (database, queue, session signing).
 * Every other line, every app-owned file and every app-generated secret is left byte-for-byte unchanged. Each
 * repaired or already-clean manager app.env is left private (0600). Credentials an app may already have read cannot be
 * un-read: rotation is a separate owner step (PRIVSEP-HANDOFF.md).
 */
export async function scrubLeakedManagerCredentials(appDataDir: string, credentials: Array<string | undefined>) {
  const secrets = new Set(credentials.filter((v): v is string => typeof v === 'string' && v.length >= 12));
  const repaired: string[] = [];
  if (!secrets.size) return repaired;
  const stores = await fs.readdir(appDataDir, { withFileTypes: true }).catch(() => []);
  for (const store of stores) {
    if (!store.isDirectory() || store.name.startsWith('.')) continue;
    const apps = await fs.readdir(path.join(appDataDir, store.name), { withFileTypes: true }).catch(() => []);
    for (const app of apps) {
      if (!app.isDirectory()) continue;
      const file = path.join(appDataDir, store.name, app.name, 'app.env');
      const st = await fs.lstat(file).catch(() => null);
      if (!st?.isFile()) continue; // never follow an app-planted link
      const text = await fs.readFile(file, 'utf8');
      const lines = text.split('\n');
      const kept = lines.filter((line) => {
        const eq = line.indexOf('=');
        if (eq < 1) return true;
        const value = line
          .slice(eq + 1)
          .trim()
          .replace(/^(['"])(.*)\1$/, '$2');
        return !secrets.has(value);
      });
      if (kept.length !== lines.length) {
        const tmp = `${file}.${process.pid}.scrub`;
        await fs.writeFile(tmp, kept.join('\n'), { mode: 0o600, flag: 'wx' });
        await fs.chown(tmp, st.uid, st.gid).catch(() => {
          // Legacy ownership is best effort; on refusal retain the private manager-created replacement.
        });
        await fs.rename(tmp, file);
        repaired.push(`${store.name}/${app.name}`);
      } else if ((st.mode & 0o077) !== 0) {
        await fs.chmod(file, 0o600);
      }
    }
  }
  return repaired;
}
