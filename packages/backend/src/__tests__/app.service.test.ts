import fs from 'node:fs';
import path from 'node:path';
import { AppService } from '@/app.service';
import { APP_DATA_DIR, APP_DIR, DATA_DIR } from '@/common/constants';
import { ConfigurationService } from '@/core/config/configuration.service';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { FsMock } from '@/tests/__mocks__/fs';
import { GithubService } from '@/utils/github/github.service';
import { faker } from '@faker-js/faker';
import { Test } from '@nestjs/testing';
import { fromPartial } from '@total-typescript/shoehorn';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mock } from 'vitest-mock-extended';
import { DOCKERODE } from '@/modules/docker/docker.module';
import YAML from 'yaml';
import { DatabaseService } from '@/core/database/database.service';
import { CacheService } from '@/core/cache/cache.service';
import { AppStoreService } from '@/modules/app-stores/app-store.service';
import { MarketplaceService } from '@/modules/marketplace/marketplace.service';
import { RepoEventsQueue } from '@/modules/queue/entities/repo-events';
import { SystemEventsQueue } from '@/modules/queue/entities/system-events';
import { AppLifecycleService } from '@/modules/app-lifecycle/app-lifecycle.service';
import { LoggerService } from '@/core/logger/logger.service';

const TRAEFIK_CONFIG = `api:
  dashboard: true
  insecure: true

entryPoints:
  web:
    address: ":80"
    forwardedHeaders:
      insecure: true
      trustedIPs:
        - "127.0.0.1/32"
  websecure:
    address: ":443"
    forwardedHeaders:
      trustedIPs:
        - "127.0.0.1/32"
    http:
      tls:
        certResolver: myresolver
`;

describe('AppService', () => {
  let appService: AppService;
  let configurationService = mock<ConfigurationService>();
  let githubService = mock<GithubService>();

  beforeEach(async () => {
    const Dockerode = vi.fn();
    const moduleRef = await Test.createTestingModule({
      providers: [
        AppService,
        FilesystemService,
        {
          provide: DOCKERODE,
          useFactory: () => Dockerode,
          inject: [],
        },
      ],
    })
      .useMocker(mock)
      .compile();

    appService = moduleRef.get(AppService);
    configurationService = moduleRef.get(ConfigurationService);
    githubService = moduleRef.get(GithubService);
  });

  describe('getVersion', () => {
    it('should return the version', async () => {
      // arrange
      const version = faker.system.semver();
      const latest = faker.system.semver();
      const body = faker.lorem.paragraph();
      configurationService.getConfig.mockReturnValueOnce(fromPartial({ version }));
      githubService.getLatestRelease.mockResolvedValueOnce({ version: latest, body });
      githubService.getReleasesSince.mockResolvedValueOnce([]);

      // act
      const result = await appService.getVersion();

      // assert
      expect(result.current).toBe(version);
      expect(result.latest).toBe(latest);
      expect(result.body).toBe(body);
    });

    it('should return version from cache if set', async () => {
      // arrange
      const version = faker.system.semver();
      const latest = faker.system.semver();
      const body = faker.lorem.paragraph();
      configurationService.getConfig.mockReturnValueOnce(fromPartial({ version }));
      githubService.getLatestRelease.mockResolvedValueOnce({ version: latest, body });
      githubService.getReleasesSince.mockResolvedValueOnce([]);

      // act
      const result = await appService.getVersion();

      // assert
      expect(result.current).toBe(version);
      expect(result.latest).toBe(latest);
      expect(result.body).toBe(body);
    });

    it('should fetch latest version from github if not in cache', async () => {
      // arrange
      const version = faker.system.semver();
      const latest = faker.system.semver();
      const body = faker.lorem.paragraph();
      configurationService.getConfig.mockReturnValueOnce(fromPartial({ version }));
      githubService.getLatestRelease.mockResolvedValueOnce({ version: latest, body });
      githubService.getReleasesSince.mockResolvedValueOnce([]);

      // act
      const result = await appService.getVersion();

      // assert
      expect(result.current).toBe(version);
      expect(result.latest).toBe(latest);
      expect(result.body).toBe(body);
      expect(githubService.getLatestRelease).toHaveBeenCalledWith('runtipi', 'runtipi');
      expect(githubService.getReleasesSince).toHaveBeenCalledWith('runtipi', 'runtipi', version);
    });

    it('should return current version if github service returns empty', async () => {
      // arrange
      const version = faker.system.semver();
      configurationService.getConfig.mockReturnValueOnce(fromPartial({ version }));
      githubService.getLatestRelease.mockResolvedValueOnce({ version: '', body: '' });
      githubService.getReleasesSince.mockResolvedValueOnce([]);

      // act
      const result = await appService.getVersion();

      // assert
      expect(result.current).toBe(version);
      expect(result.latest).toBe(version); // Should fall back to current version when github returns empty string
      expect(result.body).toBe('');
    });

    it('should return current version if github service returns empty', async () => {
      // arrange
      const version = faker.system.semver();
      configurationService.getConfig.mockReturnValueOnce(fromPartial({ version }));
      githubService.getLatestRelease.mockResolvedValueOnce({ version: '', body: '' });
      githubService.getReleasesSince.mockResolvedValueOnce([]);

      // act
      const result = await appService.getVersion();

      // assert
      expect(result.current).toBe(version);
      expect(result.latest).toBe(version); // Should fall back to current version when github returns empty string
      expect(result.body).toBe('');
    });
  });

  it('bootstrap completes without daemon-wide network pruning', async () => {
    const docker = { pruneNetworks: vi.fn().mockResolvedValue({}) };
    const database = mock<DatabaseService>();
    const cache = mock<CacheService>();
    cache.get.mockReturnValue('synthetic');
    const stores = mock<AppStoreService>();
    const marketplace = mock<MarketplaceService>();
    const repos = mock<RepoEventsQueue>();
    const system = mock<SystemEventsQueue>();
    const lifecycle = mock<AppLifecycleService>();
    const logger = mock<LoggerService>();
    configurationService.getConfig.mockReturnValue(fromPartial({ version: 'synthetic', userSettings: {}, __prod__: true }));
    const moduleRef = await Test.createTestingModule({
      providers: [AppService,
        { provide: DOCKERODE, useValue: docker },
        { provide: ConfigurationService, useValue: configurationService },
        { provide: DatabaseService, useValue: database },
        { provide: CacheService, useValue: cache },
        { provide: AppStoreService, useValue: stores },
        { provide: MarketplaceService, useValue: marketplace },
        { provide: RepoEventsQueue, useValue: repos },
        { provide: SystemEventsQueue, useValue: system },
        { provide: AppLifecycleService, useValue: lifecycle },
        { provide: LoggerService, useValue: logger }],
    }).useMocker(mock).compile();
    const subject = moduleRef.get(AppService);
    const assets = vi.spyOn(subject, 'copyAssets').mockResolvedValue();
    const tls = vi.spyOn(subject, 'generateTlsCertificates').mockResolvedValue();
    await subject.bootstrap();
    expect(docker.pruneNetworks).not.toHaveBeenCalled();
    expect(database.migrate).toHaveBeenCalledOnce();
    expect(stores.migrateLegacyRepo).toHaveBeenCalledOnce();
    expect(marketplace.initialize).toHaveBeenCalledOnce();
    expect(assets).toHaveBeenCalledOnce();
    expect(tls).toHaveBeenCalledOnce();
    expect(logger.error).not.toHaveBeenCalled();
    expect(lifecycle.restartRunningApps).not.toHaveBeenCalled();
  });

  describe('copyAssets', () => {
    it('should create base folder structure', async () => {
      // arrange
      const appDir = APP_DIR;
      const dataDir = DATA_DIR;
      const appDataDir = APP_DATA_DIR;
      const directories = { appDir, dataDir, appDataDir };
      configurationService.getConfig.mockReturnValueOnce(fromPartial({ directories, userSettings: { persistTraefikConfig: false } }));

      // act
      await appService.copyAssets();

      // assert
      expect((fs as unknown as FsMock).tree()).toMatchSnapshot();
    });

    it('should use only configured trusted proxy IPs in traefik config', async () => {
      const appDir = APP_DIR;
      const dataDir = DATA_DIR;
      const appDataDir = APP_DATA_DIR;
      const directories = { appDir, dataDir, appDataDir };
      const fsMock = fs as unknown as FsMock;
      fsMock.__applyMockFiles({
        [path.join(APP_DIR, 'assets', 'traefik', 'traefik.yml')]: TRAEFIK_CONFIG,
        [path.join(APP_DIR, 'assets', 'traefik', 'dynamic', 'dynamic.yml')]: '',
      });
      configurationService.getConfig.mockReturnValueOnce(fromPartial({ directories, userSettings: { persistTraefikConfig: false } }));
      configurationService.get.calledWith('traefik').mockReturnValueOnce({
        trustedProxyIps: ['203.0.113.10/32', '2001:db8::/32', '203.0.113.10/32'],
      });

      await appService.copyAssets();

      const output = await fs.promises.readFile(path.join(DATA_DIR, 'traefik', 'traefik.yml'), 'utf8');
      const parsed = YAML.parse(output);
      expect(parsed.entryPoints.web.forwardedHeaders.insecure).toBe(true);
      expect(parsed.entryPoints.web.forwardedHeaders.trustedIPs).toEqual(['203.0.113.10/32', '2001:db8::/32']);
      expect(parsed.entryPoints.websecure.forwardedHeaders.trustedIPs).toEqual(['203.0.113.10/32', '2001:db8::/32']);
    });

    it('should remove default trusted proxy IPs from traefik config when none are configured', async () => {
      const appDir = APP_DIR;
      const dataDir = DATA_DIR;
      const appDataDir = APP_DATA_DIR;
      const directories = { appDir, dataDir, appDataDir };
      const fsMock = fs as unknown as FsMock;
      fsMock.__applyMockFiles({
        [path.join(APP_DIR, 'assets', 'traefik', 'traefik.yml')]: TRAEFIK_CONFIG,
        [path.join(APP_DIR, 'assets', 'traefik', 'dynamic', 'dynamic.yml')]: '',
      });
      configurationService.getConfig.mockReturnValueOnce(fromPartial({ directories, userSettings: { persistTraefikConfig: false } }));
      configurationService.get.calledWith('traefik').mockReturnValueOnce({
        trustedProxyIps: [],
      });

      await appService.copyAssets();

      const output = await fs.promises.readFile(path.join(DATA_DIR, 'traefik', 'traefik.yml'), 'utf8');
      const parsed = YAML.parse(output);
      expect(parsed.entryPoints.web.forwardedHeaders.insecure).toBe(true);
      expect(parsed.entryPoints.web.forwardedHeaders.trustedIPs).toBeUndefined();
      expect(parsed.entryPoints.websecure.forwardedHeaders).toBeUndefined();
    });
  });
});
