import { AsyncLocalStorage } from 'node:async_hooks';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { MandatorySeal } from './mandatory-seal';
import { confined } from './wizard-storage';
import * as yaml from 'yaml';
const execFileStrict = promisify(execFile);
import { WizardStorage } from './wizard-storage';
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

// One representation is used for both classification and spawn. Enrolled apps
// fail closed on global prefixes/unknown actions; this is not a shell parser.
export const composeArgv = (command: string) => command.trim().split(/\s+/).filter(Boolean);
const REMEDIATION = new Set(['stop', 'down', 'kill', 'rm']);
const PASSIVE = new Set(['logs', 'ps', 'images', 'top', 'version', 'config']);

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
  public getBaseComposeArgsApp = async (appUrn: AppUrn, includeOptional = true) => {
    let isCustomConfig = false;

    const appEnv = await this.appFilesManager.getAppEnv(appUrn);
    const args: string[] = ['--env-file', appEnv.path];

    const { app } = await this.appsService.getApp(appUrn);
    const userConfigEnabled = app?.userConfigEnabled ?? true;

    // User custom env file
    const userEnvFile = includeOptional ? await this.appFilesManager.getUserEnv(appUrn) : undefined;
    if (userEnvFile?.content && userConfigEnabled) {
      isCustomConfig = true;
      args.push('--env-file', userEnvFile.path);
    }

    args.push('--project-name', appUrn.replace(':', '_'));

    const composeFile = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    args.push('-f', composeFile.path);

    // User defined overrides
    const userComposeFile = includeOptional ? await this.appFilesManager.getUserComposeFile(appUrn) : undefined;
    if (userComposeFile?.content && userConfigEnabled) {
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
    const seal = new MandatorySeal(this.config.get('directories').dataDir);
    return seal.exclusive(appUrn, () => this.prepareLocked(appUrn, seal, async (args) => {
      // Keep this public preflight's existing callback contract.
      void args;
      await apply?.();
    }));
  }

  private async prepareLocked(appUrn: AppUrn, seal: MandatorySeal, apply: (args?: string[]) => Promise<void>) {
    // No enrollment decision is captured before the cross-process fence.
    const enrollment = await seal.read(appUrn);
    const generated = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);
    if (!generated.content) throw new Error(`Missing generated Compose for ${appUrn}`);
    await this.storage.prepare(appUrn, generated.content, async (prepared) => {
      if (!enrollment && prepared !== generated.content) {
        await fs.writeFile(`${generated.path}.tmp-storage`, prepared);
        await fs.rename(`${generated.path}.tmp-storage`, generated.path);
      }
      if (enrollment) await this.sealedLaunch(appUrn, seal, yaml.parse(prepared), apply);
      else await apply();
    }, enrollment ? (doc, targets) => seal.select(enrollment, doc, targets) : undefined);
  }

  private readonly remediationContext = new AsyncLocalStorage<{ urn: AppUrn; args: string[]; active: boolean }>();

  public async composeApp(appUrn: AppUrn, command: string, beforeStart?: () => Promise<void>) {
    const argv = composeArgv(command);
    const action = argv[0] ?? '';
    // Remediation is non-activating and must work even for corrupt policies and
    // inside beforeStart while the enclosing launch owns the enrollment fence.
    const seal = new MandatorySeal(this.config.get('directories').dataDir);
    if (REMEDIATION.has(action)) {
      const context = this.remediationContext.getStore();
      if (context?.active && context.urn === appUrn) return this.runComposeApp(appUrn, argv, context.args);
      // Standalone stop/update/backup/removal must not reintroduce optional
      // host-network overrides. The sticky marker, not valid policy contents,
      // determines enrollment so corrupt-policy remediation stays available.
      const args = await seal.exists(appUrn) ? (await this.getBaseComposeArgsApp(appUrn, false)).args : undefined;
      return this.runComposeApp(appUrn, argv, args);
    }
    return seal.exclusive(appUrn, async () => {
      if (action === 'pull' && !await seal.read(appUrn)) return this.runComposeApp(appUrn, argv);
      if (action === 'up' || action === 'pull') {
        let result!: { success: boolean; stdout: string; stderr: string };
        await this.prepareLocked(appUrn, seal, async (args) => {
          if (args) {
            const context = { urn: appUrn, args, active: true };
            try { await this.remediationContext.run(context, async () => beforeStart?.()); }
            finally { context.active = false; }
          } else await beforeStart?.();
          result = await this.runComposeApp(appUrn, argv, args);
        });
        return result;
      }
      if (await seal.read(appUrn) && !PASSIVE.has(action))
        throw new Error('Sealed command requires the validated manager lifecycle');
      return this.runComposeApp(appUrn, argv);
    });
  }

  private async sealedLaunch(appUrn: AppUrn, seal: MandatorySeal, prepared: import('./wizard-storage').Compose, launch: (args: string[]) => Promise<void>) {
    const { args } = await this.getBaseComposeArgsApp(appUrn);
    const generated = await this.appFilesManager.getGeneratedDockerComposeYaml(appUrn);

    const state = await confined(seal.dataDir, 'state/mandatory-seal-launches', true);
    const dir = await fs.mkdtemp(path.join(state, 'launch-'));
    const project = ['--project-name', appUrn.replace(':', '_'), '--project-directory', path.dirname(generated.path)];
    const required = path.join(dir, 'required.yml');
    const snapshot = path.join(dir, 'effective.json');
    try {
      // Storage has already projected current grants into this trusted security
      // base. Never replace it with an older enrollment after reconciliation.
      await fs.writeFile(required, seal.overlay(appUrn, prepared), { mode: 0o600 });
      const resolve = async (input: string[]) => {
        try {
          // Dry config only: no container/network create, start, stop or pull.
          const { stdout } = await execFileStrict('docker-compose', [...input, 'config', '--format', 'json'], {
            encoding: 'utf8', timeout: 30_000, maxBuffer: 16 * 1024 * 1024,
          });
          return JSON.parse(stdout);
        } catch { throw new Error('Mandatory seal Compose validation failed; refusing lifecycle mutation'); }
      };
      const approved = await resolve([...project, '-f', required]);
      const effective = await resolve([...args, '--project-directory', path.dirname(generated.path), '-f', required]);
      seal.validateEffective(effective, approved);
      // Execute only these validated rendered bytes. Optional files/env cannot
      // race a later re-read; Compose config already escaped dollars for reinput.
      await fs.writeFile(snapshot, seal.snapshot(effective), { mode: 0o600 });
      await launch([...project, '-f', snapshot]);
    } finally {
      await fs.rm(dir, { recursive: true, force: true });
    }
  }

  private async runComposeApp(appUrn: AppUrn, argv: string[], sealedArgs?: string[]) {
    let { args, isCustomConfig } = sealedArgs
      ? { args: [...sealedArgs], isCustomConfig: false }
      : await this.getBaseComposeArgsApp(appUrn);
    args.push(...argv);

    this.logger.info(`Running docker compose with args ${args.join(' ')}`);

    const cmd = spawn('docker-compose', args);
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
