/**
 * Wizard app-to-app grants: state file + Docker network reconcile. Spec: wizard-os spec/PERMISSIONS.md.
 *
 * Everything here is a no-op unless WIZARD_APP_ISOLATION=1. Grants live in <dataDir>/state/wizard-grants.json
 * (no database change, so the image stays a drop-in for stock Runtipi). Layer 1 only: networks. The per-grant
 * token (layer 2) is not implemented yet.
 */
import fs from 'node:fs';
import path from 'node:path';
import { extractAppUrn } from '@/common/helpers/app-helpers';
import { ConfigurationService } from '@/core/config/configuration.service';
import { LoggerService } from '@/core/logger/logger.service';
import { Inject, Injectable, type OnApplicationBootstrap, type OnModuleDestroy } from '@nestjs/common';
import type { AppUrn } from '@runtipi/common/types';
import type Dockerode from 'dockerode';
import { AppsRepository } from '../apps/apps.repository';
import {
  type ContainerView,
  type GrantsFile,
  type WizardAppInfo,
  WIZARD_STORE,
  isWizardIsolationEnabled,
  isWizardStore,
  parseGrantsFile,
  parseWizardMd,
  planNetworkChanges,
  seedDefaults,
  wizardAppNetworkName,
} from './wizard-grants.logic';

// Same token as DOCKERODE in docker.module.ts (imported by value there would be a circular import).
export const WIZARD_GRANTS_DOCKERODE = 'DOCKERODE_INSTANCE';
const RECONCILE_EVERY_MS = 5 * 60 * 1000;

@Injectable()
export class WizardGrantsService implements OnApplicationBootstrap, OnModuleDestroy {
  private queue: Promise<unknown> = Promise.resolve();
  private timer?: NodeJS.Timeout;

  constructor(
    private readonly logger: LoggerService,
    private readonly configuration: ConfigurationService,
    private readonly appsRepository: AppsRepository,
    @Inject(WIZARD_GRANTS_DOCKERODE) private readonly docker: Dockerode,
  ) {}

  get enabled() {
    return isWizardIsolationEnabled();
  }

  get grantsPath() {
    return path.join(this.configuration.get('directories').dataDir, 'state', 'wizard-grants.json');
  }

  onApplicationBootstrap() {
    if (!this.enabled) return;
    this.logger.info('WIZARD_APP_ISOLATION=1: Wizard apps get their own networks; grants in state/wizard-grants.json');
    void this.reconcile();
    this.timer = setInterval(() => void this.reconcile(), RECONCILE_EVERY_MS);
    this.timer.unref?.();
  }

  onModuleDestroy() {
    if (this.timer) clearInterval(this.timer);
  }

  /**
   * Before `docker compose up` of a Wizard app: seed default grants and make sure its external network exists.
   * The provider keeps its service name as DNS alias on its own network, so a connected consumer still reaches
   * e.g. http://hermes-agent:9119 unchanged.
   */
  async beforeUp(appUrn: AppUrn) {
    if (!this.enabled || !isWizardStore(extractAppUrn(appUrn).appStoreId)) return;
    await this.serial(async () => {
      const apps = await this.installedApps();
      await this.loadAndSeed(apps);
      await this.ensureNetworks([extractAppUrn(appUrn).appName], await this.networkNames());
    });
  }

  /** After `docker compose up` (containers are new, manual network connections are gone): reconcile. */
  async afterUp(appUrn: AppUrn) {
    if (!this.enabled || !isWizardStore(extractAppUrn(appUrn).appStoreId)) return;
    await this.reconcile();
  }

  /** Create missing networks, then connect granted consumers and disconnect everything else. Never throws. */
  reconcile() {
    if (!this.enabled) return Promise.resolve();
    return this.serial(async () => {
      const apps = await this.installedApps();
      const file = await this.loadAndSeed(apps);
      const existing = await this.networkNames();
      await this.ensureNetworks(
        apps.map((a) => a.id),
        existing,
      );
      const containers = (await this.docker.listContainers({ filters: { label: ['runtipi.managed=true'] } })).map(
        (c): ContainerView => ({ id: c.Id, appUrn: c.Labels?.['runtipi.appurn'], networks: Object.keys(c.NetworkSettings?.Networks ?? {}) }),
      );
      const plan = planNetworkChanges(apps, file.grants, containers, existing);
      for (const d of plan.disconnect) {
        await this.docker
          .getNetwork(d.network)
          .disconnect({ Container: d.containerId, Force: true })
          .then(() => this.logger.info(`wizard-grants: disconnected ${d.consumer} from ${d.network}`))
          .catch((e) => this.logger.warn(`wizard-grants: disconnect ${d.consumer} from ${d.network} failed`, e));
      }
      for (const c of plan.connect) {
        await this.docker
          .getNetwork(c.network)
          .connect({ Container: c.containerId })
          .then(() => this.logger.info(`wizard-grants: connected ${c.consumer} to ${c.network}`))
          .catch((e) => this.logger.warn(`wizard-grants: connect ${c.consumer} to ${c.network} failed`, e));
      }
    });
  }

  async readGrants(): Promise<GrantsFile> {
    const text = await fs.promises.readFile(this.grantsPath, 'utf8').catch(() => null);
    return parseGrantsFile(text, Math.floor(Date.now() / 1000));
  }

  async writeGrants(file: GrantsFile) {
    await fs.promises.mkdir(path.dirname(this.grantsPath), { recursive: true });
    const tmp = `${this.grantsPath}.tmp-${process.pid}`;
    await fs.promises.writeFile(tmp, `${JSON.stringify(file, null, 2)}\n`, 'utf8');
    await fs.promises.rename(tmp, this.grantsPath);
  }

  /** Installed Wizard apps with the wizard.md fields grants need. Apps without a readable wizard.md still count. */
  async installedApps(): Promise<WizardAppInfo[]> {
    const { dataDir } = this.configuration.get('directories');
    const rows = (await this.appsRepository.getApps()).filter((a) => a.appStoreSlug === WIZARD_STORE);
    const out: WizardAppInfo[] = [];
    for (const row of rows) {
      let info: WizardAppInfo | null = null;
      for (const p of [
        path.join(dataDir, 'apps', WIZARD_STORE, row.appName, 'wizard.md'),
        path.join(dataDir, 'repos', WIZARD_STORE, 'apps', row.appName, 'wizard.md'),
      ]) {
        const text = await fs.promises.readFile(p, 'utf8').catch(() => null);
        info = text ? parseWizardMd(text) : null;
        if (info) break;
      }
      out.push(info && info.id === row.appName ? info : { id: row.appName, provides: [], uses: [], hasMcp: false });
    }
    return out;
  }

  private async loadAndSeed(apps: WizardAppInfo[]) {
    const { file, changed } = seedDefaults(await this.readGrants(), apps, Math.floor(Date.now() / 1000));
    if (changed) await this.writeGrants(file);
    return file;
  }

  private async networkNames() {
    return new Set((await this.docker.listNetworks()).map((n) => n.Name));
  }

  private async ensureNetworks(appNames: string[], existing: Set<string>) {
    for (const app of appNames) {
      const name = wizardAppNetworkName(app);
      if (existing.has(name)) continue;
      await this.docker
        .createNetwork({ Name: name, Driver: 'bridge', Internal: false, CheckDuplicate: true, Labels: { 'wizard.app': app } })
        .then(() => {
          existing.add(name);
          this.logger.info(`wizard-grants: created network ${name}`);
        })
        .catch((e) => this.logger.error(`wizard-grants: could not create network ${name}`, e));
    }
  }

  /** One reconcile at a time; errors are logged, never thrown into app lifecycle commands. */
  private serial(fn: () => Promise<void>) {
    const next = this.queue.then(fn).catch((e) => this.logger.error('wizard-grants: reconcile failed', e));
    this.queue = next;
    return next;
  }
}
