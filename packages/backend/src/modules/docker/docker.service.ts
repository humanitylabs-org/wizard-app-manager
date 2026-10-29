import { spawn } from 'node:child_process';
import { WizardStorage } from './wizard-storage';
import { enforceAppNetworks } from './wizard-networks';
import { assertFinalHostPolicy, composeEnv, injectedBinds, MUTATING, readHostApprovals, type GuardContext } from './wizard-host-guard';
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

  public async prepareApp(appUrn: AppUrn, apply?: () => Promise<void>) {
    const generated = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    if (!generated.content) throw new Error(`Missing generated Compose for ${appUrn}`);
    await this.storage.prepare(appUrn, generated.content, async (prepared) => {
      // App networks: every start of a Wizard app, from wam, the App Store views or Runtipi's UI (wizard-networks.ts).
      const app = await Promise.resolve().then(() => this.appsRepository.getAppByUrn(appUrn)).catch(() => undefined);
      let publishedServices: string[] = [];
      if (appUrn.endsWith(':wizardapps') && (app?.exposed === true || app?.exposedLocal === true)) {
        const source = await this.appFilesManager.getSourceDockerComposeYaml(appUrn);
        if (!source.content) throw new Error(`Missing source Compose for published ${appUrn}`);
        const services = servicesForArchitecture(source.content, this.config.get('architecture'));
        publishedServices = Object.entries(services)
          .filter(([, service]) => service['x-runtipi']?.is_main === true && service['x-runtipi']?.internal_port && service.network_mode === undefined)
          .map(([name]) => name);
      }
      const enforced = await enforceAppNetworks(appUrn, prepared, this.config.get('directories').dataDir, publishedServices);
      if (enforced !== generated.content) {
        await fs.writeFile(`${generated.path}.tmp-storage`, enforced);
        await fs.rename(`${generated.path}.tmp-storage`, generated.path);
      }
      await apply?.();
    });
  }

  public async composeApp(appUrn: AppUrn, command: string, beforeStart?: () => Promise<void>) {
    if (command.split(' ')[0] === 'up') {
      let result!: { success: boolean; stdout: string; stderr: string };
      await this.prepareApp(appUrn, async () => {
        // Validate storage/shares/bridge before a restart can stop anything,
        // holding the grant lock continuously through down and up.
        await beforeStart?.();
        result = await this.runComposeApp(appUrn, command);
      });
      return result;
    }
    return this.runComposeApp(appUrn, command);
  }

  private async runComposeApp(appUrn: AppUrn, command: string) {
    let { args, isCustomConfig } = await this.getBaseComposeArgsApp(appUrn);
    // Final privileged consumer: Compose's own merged/interpolated model of THIS
    // invocation (generated recipe, user override, every env file) must hold no
    // host access the App Manager did not accept. Checked before Docker changes.
    if (MUTATING.has(command.split(' ')[0] ?? '')) await this.assertHostPolicy(appUrn, args);
    args.push(...command.split(' '));
    args = args.filter(Boolean);

    this.logger.info(`Running docker compose with args ${args.join(' ')}`);

    const cmd = spawn('docker-compose', args, { env: composeEnv() });
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

  private async composeConfig(args: string[]): Promise<unknown> {
    const cmd = spawn('docker-compose', [...args, 'config', '--format', 'json'], { env: composeEnv() });
    const stdout: Buffer[] = [];
    const stderr: string[] = [];
    let size = 0;
    const code = await new Promise((resolve, reject) => {
      cmd.once('error', reject);
      cmd.stdout.on('data', (d: Buffer) => {
        size += d.length;
        if (size > 8 * 1024 * 1024) cmd.kill('SIGKILL');
        else stdout.push(d);
      });
      cmd.stderr.on('data', (d) => stderr.push(String(d)));
      cmd.on('close', resolve);
    });
    if (code !== 0) throw new Error(`Compose model unavailable; refusing to start (${stderr.join('').trim().slice(-300)})`);
    return JSON.parse(Buffer.concat(stdout).toString('utf8'));
  }

  public async assertHostPolicy(appUrn: AppUrn, baseArgs: string[]) {
    const { appName, appStoreId } = extractAppUrn(appUrn);
    const { directories, rootFolderHost, userSettings } = this.config.getConfig();
    const appDataRoot = `${userSettings.appDataPath || rootFolderHost}/app-data`;
    const generated = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    const source = await this.appFilesManager.getSourceDockerComposeYaml(appUrn).catch(() => null);
    // Without the installed recipe nothing counts as grant-injected (fail closed).
    const recipe = source?.content ? servicesForArchitecture(source.content, this.config.get('architecture')) : null;
    const ctx: GuardContext = {
      appId: appName,
      store: appStoreId,
      appDataDir: path.posix.join(appDataRoot, appStoreId, appName),
      filesRoot: path.posix.join(rootFolderHost, 'media'),
      sourceRoot: path.posix.join(appDataRoot, '.wizard-source', appStoreId),
      appDataRoot,
      envDirs: [path.posix.join(directories.appDataDir, appStoreId, appName), path.posix.join(directories.dataDir, 'user-config', appStoreId, appName)],
      injected: recipe && generated.content ? injectedBinds(yaml.parse(generated.content), recipe) : new Set(),
    };
    assertFinalHostPolicy(await this.composeConfig(baseArgs), ctx, await readHostApprovals(directories.dataDir));
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
