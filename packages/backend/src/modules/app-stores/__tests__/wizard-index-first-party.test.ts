import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ConfigurationService } from '@/core/config/configuration.service';
import type { AppStore } from '@/core/database/drizzle/types';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import { allowedPublisher, WizardIndexStore } from '../wizard-index-store';

// wizard-os#138: first-party apps only until wizard-os#133.
const [first, third] = [
  { repo: 'humanitylabs-org/wisdom', publisher: 'humanitylabs-org', id: 'wisdom', name: 'Wisdom', summary: '' },
  { repo: 'stranger/evil', publisher: 'stranger', id: 'evil', name: 'Evil', summary: '' },
] as const;
const apps = [first, third];

describe('first-party Store', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete process.env.WAM_PUBLISHERS;
  });
  it('decides by GitHub owner, with an owner override', () => {
    expect(allowedPublisher(first, '')).toBe(true);
    expect(allowedPublisher(third, '')).toBe(false);
    expect(allowedPublisher({ repo: 'OscarHenryCollins/browser' }, '')).toBe(true);
    expect(allowedPublisher(third, '*')).toBe(true);
    expect(allowedPublisher(third, 'stranger')).toBe(true);
    expect(allowedPublisher(first, 'stranger')).toBe(false);
  });
  it('search hides third-party apps', async () => {
    const logger = { debug: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() } as unknown as LoggerService;
    const configuration = { get: vi.fn(), getConfig: vi.fn().mockReturnValue({}) } as unknown as ConfigurationService;
    const store = new WizardIndexStore(configuration, new FilesystemService(logger), logger, {
      slug: 'wizardapps',
      url: 'https://index.test',
    } as AppStore);
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ apps, next_offset: null })));
    const res = await store.search({ search: '' });
    expect(res.data.map((a) => a.id)).toEqual(['wisdom']);
  });
});
