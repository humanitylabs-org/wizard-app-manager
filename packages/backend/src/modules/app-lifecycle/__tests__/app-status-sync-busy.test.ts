import { describe, expect, it, vi } from 'vitest';
import { AsyncMutex } from '@/utils/mutex/async-mutex';
import { AppStatusSyncService } from '../app-status-sync.service';

// WAM#711: the 5-minute status sync used to overwrite `updating` from Docker while a big update was still pulling or
// backing up (old containers running, or all stopped), and callers then acted on that guess.
const make = (status: string, minutesAgo: number, mutex: AsyncMutex) => {
  const app = { id: 1, appName: 'messenger', appStoreSlug: 'wizardapps', status, updatedAt: Math.floor(Date.now() / 1000) - minutesAgo * 60 };
  const appRepository = { getApps: vi.fn(async () => [app]), updateAppById: vi.fn(async () => {}) };
  const docker = { listContainers: vi.fn(async () => [{ Id: 'c', State: 'exited', Status: 'Exited (137)', Labels: { 'runtipi.appurn': 'messenger:wizardapps' } }]) };
  const configuration = { get: vi.fn(() => ({ eventsTimeout: 5 })) };
  const logger = { debug: vi.fn(), warn: vi.fn(), info: vi.fn(), error: vi.fn() };
  const svc = new AppStatusSyncService(
    logger as never,
    appRepository as never,
    { emit: vi.fn() } as never,
    { onEvent: vi.fn() } as never,
    configuration as never,
    docker as never,
    mutex,
  );
  return { svc, appRepository };
};

describe('app status sync and running commands (WAM#711)', () => {
  it('leaves an app alone while its lifecycle command still runs, however long', async () => {
    const mutex = new AsyncMutex();
    const release = await mutex.acquire('messenger:wizardapps');
    const t = make('updating', 120, mutex);
    await t.svc.syncAllAppStatuses();
    expect(t.appRepository.updateAppById).not.toHaveBeenCalled();
    release();
  });

  it('gives an update 30 minutes before guessing from Docker', async () => {
    const t = make('updating', 10, new AsyncMutex());
    await t.svc.syncAllAppStatuses();
    expect(t.appRepository.updateAppById).not.toHaveBeenCalled();
  });

  it('still repairs an app stuck in updating with nothing running', async () => {
    const t = make('updating', 45, new AsyncMutex());
    await t.svc.syncAllAppStatuses();
    expect(t.appRepository.updateAppById).toHaveBeenCalledWith(1, { status: 'stopped' });
  });
});
