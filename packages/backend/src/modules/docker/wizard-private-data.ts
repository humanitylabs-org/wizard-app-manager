/** Prepare the platform-created /data bind before a non-root main process starts.
 * Chown only its root, never recordings, nested data, shares or another app's tree.
 * Docker's missing-bind creation and Runtipi chmod cannot establish ownership:
 * Recorder legitimately chmods its private data root and otherwise exits EPERM.
 */
import fs from 'node:fs/promises';

type App = { data: string; compose: { services: Record<string, any> } };
type Confined = (base: string, relative: string, create: boolean) => Promise<string>;

export async function prepareMainPrivateData(app: App, confined: Confined, io = fs): Promise<void> {
  const mains = Object.values(app.compose.services).filter((s) => s['x-runtipi']?.is_main === true);
  if (mains.length !== 1) return;
  const main = mains[0];
  const user = String(main.user ?? '0:0');
  if (!/^\d+(?::\d+)?$/.test(user)) return; // Never guess a named user's uid.
  const ids = user.split(':');
  const uid = Number(ids[0]);
  const gid = Number(ids[1] ?? ids[0]);
  if (!uid || !Number.isSafeInteger(uid) || !Number.isSafeInteger(gid)) return;
  const ownsData = (main.volumes ?? []).some((v: any) => {
    if (typeof v === 'string') {
      const [source, target, flags = ''] = v.split(':');
      return source === '${APP_DATA_DIR}/data' && target === '/data' && !flags.split(',').includes('ro');
    }
    return v?.type === 'bind' && v.source === '${APP_DATA_DIR}/data' && v.target === '/data' && !v.read_only;
  });
  if (!ownsData) return;
  // Existing shared roots retain the provider's separate ownership contract.
  if (Object.values(app.compose.services).some((s) => s !== main && (s.volumes ?? []).some((v: any) =>
    (typeof v === 'string' ? v.split(':')[0] : v?.source) === '${APP_DATA_DIR}/data'))) return;
  const folder = await confined(app.data, 'data', true);
  const st = await io.lstat(folder);
  if (!st.isDirectory() || st.isSymbolicLink()) throw new Error('Private data root must be a real directory');
  if (st.uid === uid && st.gid === gid) return;
  // Repair only the platform-created root:root case. Other existing owners need
  // an explicit migration, not a recursive or silent ownership takeover.
  if (st.uid !== 0 || st.gid !== 0) throw new Error('Private data root has an unexpected owner; refusing ownership takeover');
  await io.chown(folder, uid, gid);
}
