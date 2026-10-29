import { spawn } from 'node:child_process';
import os from 'node:os';
import { teardownApp } from './app-teardown';
import { withHostAuthority } from './host-policy-lock';
import { verifyBindSources, executableModel, assertNoWritableBindAncestors } from './compose-consumption';
import { runBindHelper, reapBindHelper, helperRun } from './bind-helper';
import { constants } from 'node:fs';
import { WizardStorage } from './wizard-storage';
import { enforceAppNetworks } from './wizard-networks';
import {
  assertFinalHostPolicy,
  composeEnv,
  MUTATING,
  readHostApprovals,
  unsupportedFeatures,
  HostAccessRefused,
  type GuardContext,
} from './wizard-host-guard';
import { extractAppUrn } from '@/common/helpers/app-helpers';
import * as yaml from 'yaml';
import { servicesForArchitecture } from './builders/compose.builder';
import fs from 'node:fs/promises';
import path from 'node:path';
import { ConfigurationService } from '@/core/config/configuration.service';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import { LoggerService } from '@/core/logger/logger.service';
import { Injectable, InternalServerErrorException, type OnApplicationBootstrap, type OnModuleDestroy } from '@nestjs/common';
import type { AppUrn } from '@runtipi/common/types';
import * as Sentry from '@sentry/nestjs';
import { AppFilesManager } from '../apps/app-files-manager';
import { AppsService } from '../apps/apps.service';
import { AppsRepository } from '../apps/apps.repository';

type ValidatedCompose = { args: string[]; input: string };

@Injectable()
export class DockerService implements OnApplicationBootstrap, OnModuleDestroy {
  private grantsTimer?: NodeJS.Timeout;
  onApplicationBootstrap() {
    const run = () => this.storage.reconcile().catch((e) => this.logger.error('App grants reconcile failed', e));
    void run();
    this.grantsTimer = setInterval(() => void run(), 5 * 60 * 1000);
    this.grantsTimer.unref?.();
  }
  onModuleDestroy() {
    if (this.grantsTimer) clearInterval(this.grantsTimer);
  }
  constructor(
    private readonly logger: LoggerService,
    private readonly config: ConfigurationService,
    private readonly appFilesManager: AppFilesManager,
    private readonly filesystem: FilesystemService,
    private readonly appsService: AppsService,
    private readonly appsRepository: AppsRepository,
  ) {}

  /**
   * Get the base compose args for an app
   * @param {string} appUrn - App name
   */
  public getBaseComposeArgsApp = async (appUrn: AppUrn) => {
    let isCustomConfig = false;

    const appEnv = await this.appFilesManager.getAppEnv(appUrn);
    const args: string[] = ['--env-file', appEnv.path];

    const { app } = await this.appsService.getApp(appUrn);
    const userConfigEnabled = app?.userConfigEnabled ?? true;

    // User custom env file
    const userEnvFile = await this.appFilesManager.getUserEnv(appUrn);
    if (userEnvFile.content && userConfigEnabled) {
      isCustomConfig = true;
      args.push('--env-file', userEnvFile.path);
    }

    args.push('--project-name', appUrn.replace(':', '_'));

    const composeFile = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    args.push('-f', composeFile.path);

    // User defined overrides
    const userComposeFile = await this.appFilesManager.getUserComposeFile(appUrn);
    if (userComposeFile.content && userConfigEnabled) {
      isCustomConfig = true;
      args.push('--file', userComposeFile.path);
    }

    return { args, isCustomConfig };
  };

  public getBaseComposeArgsRuntipi = async () => {
    const { dataDir } = this.config.get('directories');
    const args: string[] = ['--env-file', path.join(dataDir, '.env')];

    args.push('--project-name', 'runtipi');

    const composeFile = path.join(dataDir, 'docker-compose.yml');
    args.push('-f', composeFile);

    // User defined overrides
    const userComposeFile = path.join(dataDir, 'user-config', 'tipi-compose.yml');
    if (await this.filesystem.isFile(userComposeFile)) {
      args.push('--file', userComposeFile);
    }

    return { args };
  };

  /**
   * Helpers to execute docker compose commands
   * @param {string} appUrn - App name
   * @param {string} command - Command to execute
   */
  private storageInstance?: WizardStorage;
  get storage() {
    if (!this.storageInstance)
      this.storageInstance = new WizardStorage(
        this.config.get('directories'),
        () => this.appsRepository.getApps(),
        undefined,
        undefined,
        (message) => this.logger.warn(message),
      );
    return this.storageInstance;
  }

  public async prepareApp(appUrn: AppUrn, apply?: (prepared: string, grantedBinds?: Set<string>) => Promise<void>) {
    const generated = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    if (!generated.content) throw new Error(`Missing generated Compose for ${appUrn}`);
    await this.storage.prepare(appUrn, generated.content, async (prepared, grantedBinds) => {
      // App networks: every start of a Wizard app, from wam, the App Store views or Runtipi's UI (wizard-networks.ts).
      const app = await Promise.resolve()
        .then(() => this.appsRepository.getAppByUrn(appUrn))
        .catch(() => undefined);
      let publishedServices: string[] = [];
      if (appUrn.endsWith(':wizardapps') && (app?.exposed === true || app?.exposedLocal === true)) {
        const source = await this.appFilesManager.getSourceDockerComposeYaml(appUrn);
        if (!source.content) throw new Error(`Missing source Compose for published ${appUrn}`);
        const services = servicesForArchitecture(source.content, this.config.get('architecture'));
        publishedServices = Object.entries(services)
          .filter(
            ([, service]) => service['x-runtipi']?.is_main === true && service['x-runtipi']?.internal_port && service.network_mode === undefined,
          )
          .map(([name]) => name);
      }
      const enforced = await enforceAppNetworks(appUrn, prepared, this.config.get('directories').dataDir, publishedServices);
      if (enforced !== generated.content) {
        await fs.writeFile(`${generated.path}.tmp-storage`, enforced);
        await fs.rename(`${generated.path}.tmp-storage`, generated.path);
      }
      await apply?.(enforced, grantedBinds);
    });
  }

  public async composeApp(appUrn: AppUrn, command: string, beforeStart?: () => Promise<void>) {
    return withHostAuthority(this.config.get('directories').dataDir, async () => {
      if (['down', 'stop'].includes(command.split(' ')[0] ?? '')) {
        // Quarantine never prevents risk-reducing stop; cleanup errors still propagate.
        const result = await teardownApp(appUrn, command);
        await reapBindHelper(this.config.get('directories').dataDir);
        return result;
      }
      if (!MUTATING.has(command.split(' ')[0] ?? '') && command.split(' ')[0] !== 'pull')
        throw new Error('Unsupported app lifecycle command');
      await reapBindHelper(this.config.get('directories').dataDir);
      if (MUTATING.has(command.split(' ')[0] ?? '')) {
        let result!: { success: boolean; stdout: string; stderr: string };
        await this.prepareApp(appUrn, async (prepared, grantedBinds) => {
          const validated = await this.validatedCompose(appUrn, prepared, grantedBinds);
          // The final policy AND immutable executable model exist before down.
          await beforeStart?.();
          result = await this.runComposeApp(appUrn, command, validated);
        });
        return result;
      }
      return this.runComposeApp(appUrn, command);
    });
  }

  private async runComposeApp(appUrn: AppUrn, command: string, validated?: ValidatedCompose) {
    if (!validated && MUTATING.has(command.split(' ')[0] ?? '')) throw new Error('Validated Compose model required');
    const pinned = validated;
    let { args, isCustomConfig } = pinned ? { args: [...pinned.args], isCustomConfig: true } : await this.getBaseComposeArgsApp(appUrn);
    args.push(...command.split(' '));
    args = args.filter(Boolean);

    this.logger.info(`Running docker compose with args ${args.join(' ')}`);

    const cmd = spawn('docker-compose', args, { env: composeEnv() });
    cmd.stdin.on('error', () => {
      /* command exit/error below is authoritative */
    });
    if (pinned) cmd.stdin.end(pinned.input);
    const stdout: string[] = [];
    const stderr: string[] = [];

    const exitCode = await new Promise((resolve, reject) => {
      cmd.once('error', reject);
      cmd.stdout.on('data', (data) => {
        this.logger.debug(`docker-compose: ${String(data).trim()}`);
        stdout.push(String(data).trim());
      });
      cmd.stderr.on('data', (data) => {
        this.logger.debug(`docker-compose: ${String(data).trim()}`);
        stderr.push(String(data).trim());
      });
      cmd.on('close', resolve);
    });

    if (exitCode !== 0) {
      this.logger.info(`Docker-compose exited with code ${exitCode}`);
      if (isCustomConfig) {
        this.logger.warn('User-config detected, please make sure your configuration is correct before opening an issue');
      }
      const error = stderr.pop();
      throw new Error(error);
    }

    return { success: true, stdout: stdout.join(''), stderr: stderr.join('') };
  }

  /** Snapshot every command input under the authority lock, then execute only
   * the checked in-memory Compose model on stdin. Never reread override/env. */
  private async validatedCompose(appUrn: AppUrn, prepared?: string, grantedBinds = new Set<string>()): Promise<ValidatedCompose> {
    const { args: original } = await this.getBaseComposeArgsApp(appUrn);
    const { dataDir } = this.config.get('directories');
    const temp = await fs.mkdtemp(path.join(dataDir, 'user-config', '.compose-guard-'));
    await fs.chmod(temp, 0o700);

    try {
      const args = [...original];
      let projectDir = '';
      for (let i = 0; i < args.length - 1; i++) {
        if (!['-f', '--file', '--env-file'].includes(args[i]!)) continue;
        const compose = args[i] !== '--env-file';
        const file = args[++i]!;
        const content = compose && !projectDir && prepared !== undefined ? prepared : await fs.readFile(file, 'utf8');
        if (Buffer.byteLength(content) > 8 * 1024 * 1024) throw new Error('Compose input exceeds policy limit');
        if (compose) {
          projectDir ||= path.dirname(file);
          const unsupported = unsupportedFeatures(yaml.parse(content));
          if (unsupported.length) throw new HostAccessRefused(appUrn, unsupported);
        }
        const snap = path.join(temp, String(i));
        await fs.writeFile(snap, content, { mode: 0o600, flag: 'wx' });
        args[i] = snap;
      }
      args.push('--project-directory', projectDir);
      const { model, ctx } = await this.assertHostPolicy(appUrn, args, grantedBinds);
      const bindDeadline = AbortSignal.timeout(30_000);
      await this.assertVolumeIdentity(model, appUrn.replace(':', '_'), bindDeadline);
      const managerUid = (await fs.stat(path.join(dataDir, '.env'))).uid;
      if (Object.values(model.services ?? {}).some((s: any) => (s.volumes ?? []).some((v: any) => v.type === 'bind'))) {
        // Persisted binds can return without admission on daemon restart (always
        // policy). Stopped deployments retain their overlap fence until removed.
        const ids = (await helperRun(['ps', '-a', '-q', '--no-trunc'], bindDeadline)).stdout.trim().split(/\s+/).filter(Boolean);
        const existing: string[] = [];
        const existingSources: string[] = [];
        for (const id of ids) {
          const info = JSON.parse((await helperRun(['inspect', '--type', 'container', id], bindDeadline)).stdout)[0];
          // Our own root/manager mapping is trusted control-plane custody, not an app.
          if (info.Config?.Hostname === os.hostname() && info.Id.startsWith(os.hostname())) continue;
          for (const m of info.Mounts ?? []) if (m.Type === 'bind') {
            existingSources.push(m.Source);
            if (m.RW) existing.push(m.Source);
          }
        }
        assertNoWritableBindAncestors(model, existing, existingSources);
      }
      await verifyBindSources(
        model,
        ctx,
        managerUid,
        (_file, argv) => runBindHelper(dataDir, argv, undefined, bindDeadline),
        async (source) => {
          // Missing ordinary data directories may be created only through an
          // attested backend mapping of the ancestry the daemon just checked.
          const self = JSON.parse((await helperRun(['inspect', '--type', 'container', os.hostname()], bindDeadline)).stdout)[0];
          if (!self?.State?.Running || self.Config?.Hostname !== os.hostname()) throw new Error('Cannot attest backend namespace');
          const localRoot = this.config.get('directories').appDataDir;
          if (!(self.Mounts ?? []).some((m: any) => m.Type === 'bind' && m.Source === ctx.appDataRoot && m.Destination === localRoot))
            throw new HostAccessRefused(appUrn, ['daemon/backend mount mapping is not attested']);
          const local = path.join(localRoot, path.posix.relative(ctx.appDataRoot!, source));
          // Only a missing direct child endpoint; never touch an existing payload
          // or create through writable intermediate directories. fchmod pins the
          // newly created directory inode and does not re-resolve a mutable path.
          if (path.dirname(source) !== ctx.appDataDir) throw new HostAccessRefused(appUrn, ['nested data provisioning is unsupported']);
          await fs.mkdir(local, { mode: 0o777 });
          const endpoint = await fs.open(local, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
          try { await endpoint.chmod(0o777); } finally { await endpoint.close(); }
        },
      );
      const input = JSON.stringify(executableModel(model));
      return {
        args: ['--env-file', '/dev/null', '--project-name', appUrn.replace(':', '_'), '--project-directory', projectDir, '--file', '-'],
        input,
      };
    } finally {
      await fs.rm(temp, { recursive: true, force: true });
    }
  }

  private async assertVolumeIdentity(model: any, project: string, signal = AbortSignal.timeout(30_000)) {
    for (const [key, spec] of Object.entries(model.volumes ?? {}) as [string, any][]) {
      const name = `${project}_${key}`;
      if (spec?.name !== name || spec.external || (spec.driver && spec.driver !== 'local') || Object.keys(spec.driver_opts ?? {}).length)
        throw new HostAccessRefused(project, ['named volume is not project-private']);
      let existing: any;
      try {
        existing = JSON.parse((await helperRun(['volume', 'inspect', name], signal)).stdout)[0];
      } catch (e: any) {
        if (/no such volume/i.test(String(e.stderr))) continue;
        throw e;
      }
      if (
        existing.Driver !== 'local' ||
        Object.keys(existing.Options ?? {}).length ||
        existing.Labels?.['com.docker.compose.project'] !== project ||
        existing.Labels?.['com.docker.compose.volume'] !== key
      )
        throw new HostAccessRefused(project, ['existing named volume has foreign identity or host driver options']);
    }
  }

  private async composeConfig(args: string[]): Promise<unknown> {
    const cmd = spawn('docker-compose', [...args, 'config', '--format', 'json'], { env: composeEnv() });
    const stdout: Buffer[] = [];
    const stderr: string[] = [];
    let size = 0;
    let code: unknown;
    const timer = setTimeout(() => cmd.kill('SIGKILL'), 20_000);
    try {
      code = await new Promise((resolve, reject) => {
        cmd.once('error', reject);
        cmd.stdout.on('data', (d: Buffer) => {
          size += d.length;
          if (size > 8 * 1024 * 1024) cmd.kill('SIGKILL');
          else stdout.push(d);
        });
        cmd.stderr.on('data', (d) => stderr.push(String(d)));
        cmd.on('close', resolve);
      });
    } finally { clearTimeout(timer); }
    if (code !== 0) throw new Error(`Compose model unavailable; refusing to start (${stderr.join('').trim().slice(-300)})`);
    return JSON.parse(Buffer.concat(stdout).toString('utf8'));
  }

  public async assertHostPolicy(appUrn: AppUrn, baseArgs: string[], grantedBinds = new Set<string>()) {
    const { appName, appStoreId } = extractAppUrn(appUrn);
    const { directories, rootFolderHost, userSettings } = this.config.getConfig();
    const appDataRoot = `${userSettings.appDataPath || rootFolderHost}/app-data`;

    const ctx: GuardContext = {
      appId: appName,
      store: appStoreId,
      appDataDir: path.posix.join(appDataRoot, appStoreId, appName),
      filesRoot: path.posix.join(rootFolderHost, 'media'),
      sourceRoot: path.posix.join(appDataRoot, '.wizard-source', appStoreId),
      appDataRoot,
      envDirs: [
        path.posix.join(directories.appDataDir, appStoreId, appName),
        path.posix.join(directories.dataDir, 'user-config', appStoreId, appName),
      ],
      // Only tuples freshly returned by WizardStorage under its grant lock.
      // Never infer grant authority from generated files or app-supplied metadata.
      injected: grantedBinds,
    };
    const model: any = await this.composeConfig(baseArgs);
    assertFinalHostPolicy(model, ctx, await readHostApprovals(directories.dataDir));
    return { model, ctx };
  }

  public getLogsStream = async (maxLines: number, appUrn?: AppUrn) => {
    try {
      const { args } = appUrn ? await this.getBaseComposeArgsApp(appUrn) : await this.getBaseComposeArgsRuntipi();

      args.push('logs', '--follow', '-n', maxLines.toString());

      const logs = spawn('docker-compose', args, { stdio: 'pipe' });

      logs.on('error', () => {
        logs.kill('SIGINT');
      });

      return {
        on: logs.stdout.on.bind(logs.stdout),
        kill: () => logs.kill('SIGINT'),
      };
    } catch (error) {
      this.logger.error('Error getting log stream', error);
      Sentry.captureException(error, { tags: { source: 'docker log stream', appUrn } });
      throw new InternalServerErrorException('Error getting log stream');
    }
  };
}
