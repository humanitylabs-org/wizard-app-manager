import { describe, expect, it } from 'vitest';
import {
  type Grant,
  type WizardAppInfo,
  allowedConsumers,
  defaultGrantsFor,
  emptyGrantsFile,
  parseGrantsFile,
  parseWizardMd,
  planNetworkChanges,
  seedDefaults,
} from '../wizard-grants.logic';

const app = (id: string, over: Partial<WizardAppInfo> = {}): WizardAppInfo => ({ id, provides: [], uses: [], hasMcp: false, ...over });

// The live box today
const hermes = app('hermes-agent', { type: 'agent', category: 'agent', provides: ['agent'], hasMcp: true });
const frontend = app('hermes-frontend-3', { type: 'view', category: 'interface', provides: ['agent-ui'], uses: ['hermes-agent'] });
const transcriber = app('transcriber', { type: 'service', category: 'system', provides: ['transcription'], hasMcp: true });
const secretDrop = app('secret-drop', { type: 'service', category: 'system', provides: ['secret-drop'], hasMcp: true });
const box = [hermes, frontend, transcriber, secretDrop];

const grant = (from: string, to: string): Grant => ({ from, to, reason: 'owner', approved: 'owner', created_at: 1 });

describe('parseWizardMd', () => {
  it('reads id, category, provides, uses and mcp', () => {
    const text = '---\nid: defleur-video\ntype: service\nprovides: [video-editing]\nuses: [transcription, files]\nmcp:\n  port: 8787\n---\n# body\n';
    expect(parseWizardMd(text)).toEqual({
      id: 'defleur-video',
      type: 'service',
      category: undefined,
      provides: ['video-editing'],
      uses: ['transcription', 'files'],
      hasMcp: true,
    });
  });

  it('returns null without front matter', () => {
    expect(parseWizardMd('# no front matter')).toBeNull();
  });
});

describe('defaultGrantsFor', () => {
  it('gives an agent every installed app with an MCP (agent→app first)', () => {
    expect(defaultGrantsFor(hermes, box)).toEqual([
      { from: 'hermes-agent', to: 'transcriber', reason: 'agent' },
      { from: 'hermes-agent', to: 'secret-drop', reason: 'agent' },
    ]);
  });

  it('grants a new app with an MCP to every installed agent', () => {
    expect(defaultGrantsFor(transcriber, box)).toEqual([{ from: 'hermes-agent', to: 'transcriber', reason: 'agent' }]);
  });

  it('turns uses: into grants, app id first, otherwise a slot', () => {
    expect(defaultGrantsFor(frontend, box)).toEqual([{ from: 'hermes-frontend-3', to: 'hermes-agent', reason: 'uses' }]);
    const video = app('defleur-video', { uses: ['transcription', 'video-editing'] });
    expect(defaultGrantsFor(video, [...box, video])).toEqual([
      { from: 'defleur-video', to: 'slot:transcription', reason: 'uses' },
      { from: 'defleur-video', to: 'slot:video-editing', reason: 'uses' },
    ]);
  });
});

describe('seedDefaults', () => {
  it('seeds each app once and never re-adds a denied or revoked default', () => {
    const first = seedDefaults(emptyGrantsFile(1), box, 1);
    expect(first.changed).toBe(true);
    expect(first.file.seeded).toEqual(['hermes-agent', 'hermes-frontend-3', 'secret-drop', 'transcriber']);
    expect(first.file.grants.map((g) => `${g.from}>${g.to}`).sort()).toEqual([
      'hermes-agent>secret-drop',
      'hermes-agent>transcriber',
      'hermes-frontend-3>hermes-agent',
    ]);

    // owner revokes hermes-agent > secret-drop
    const revoked = {
      ...first.file,
      grants: first.file.grants.filter((g) => g.to !== 'secret-drop'),
      denied: [{ from: 'hermes-agent', to: 'secret-drop' }],
    };
    expect(seedDefaults(revoked, box, 2).changed).toBe(false);

    // a new app with an MCP: agent gets it, the denied pair stays out
    const files = app('files', { provides: ['files'], hasMcp: true });
    const again = seedDefaults(revoked, [...box, files], 3);
    expect(again.file.grants.map((g) => `${g.from}>${g.to}`).sort()).toEqual([
      'hermes-agent>files',
      'hermes-agent>transcriber',
      'hermes-frontend-3>hermes-agent',
    ]);
  });

  it('round-trips through the file format', () => {
    const { file } = seedDefaults(emptyGrantsFile(1), box, 1);
    expect(parseGrantsFile(JSON.stringify(file), 9)).toEqual(file);
    expect(parseGrantsFile(null, 9)).toEqual(emptyGrantsFile(9));
  });
});

describe('allowedConsumers', () => {
  it('resolves slot grants to every app filling the slot and ignores self and unknown apps', () => {
    const whisper = app('whisper-alt', { provides: ['transcription'], hasMcp: true });
    const out = allowedConsumers(
      [grant('hermes-frontend-3', 'slot:transcription'), grant('transcriber', 'transcriber'), grant('gone', 'hermes-agent')],
      [...box, whisper],
    );
    expect([...(out.get('transcriber') ?? [])]).toEqual(['hermes-frontend-3']);
    expect([...(out.get('whisper-alt') ?? [])]).toEqual(['hermes-frontend-3']);
    expect(out.has('hermes-agent')).toBe(false);
  });
});

describe('planNetworkChanges', () => {
  const nets = new Set(['wizard-app-hermes-agent', 'wizard-app-hermes-frontend-3', 'wizard-app-transcriber', 'wizard-app-secret-drop']);
  const containers = [
    { id: 'agent-front', appUrn: 'hermes-agent:wizardapps', networks: ['wizard-app-hermes-agent', 'hermes-agent_wizardapps_network'] },
    { id: 'agent-db', appUrn: 'hermes-agent:wizardapps', networks: ['hermes-agent_wizardapps_network'] },
    { id: 'frontend', appUrn: 'hermes-frontend-3:wizardapps', networks: ['wizard-app-hermes-frontend-3'] },
    { id: 'transcriber', appUrn: 'transcriber:wizardapps', networks: ['wizard-app-transcriber', 'wizard-app-secret-drop'] },
    { id: 'nextcloud', appUrn: 'nextcloud:migrated', networks: ['runtipi_tipi_main_network', 'wizard-app-transcriber'] },
  ];

  it('connects granted front containers and disconnects everything else', () => {
    const grants = [grant('hermes-frontend-3', 'hermes-agent'), grant('hermes-agent', 'transcriber')];
    const plan = planNetworkChanges(box, grants, containers, nets);
    expect(plan.connect).toEqual([
      { network: 'wizard-app-hermes-agent', containerId: 'frontend', consumer: 'hermes-frontend-3' },
      { network: 'wizard-app-transcriber', containerId: 'agent-front', consumer: 'hermes-agent' },
    ]);
    expect(plan.disconnect).toEqual([
      { network: 'wizard-app-transcriber', containerId: 'nextcloud', consumer: 'nextcloud:migrated' },
      { network: 'wizard-app-secret-drop', containerId: 'transcriber', consumer: 'transcriber' },
    ]);
  });

  it('revoking a grant disconnects the consumer', () => {
    const connected = containers.map((c) => (c.id === 'frontend' ? { ...c, networks: [...c.networks, 'wizard-app-hermes-agent'] } : c));
    const plan = planNetworkChanges(box, [], connected, nets);
    expect(plan.disconnect).toContainEqual({ network: 'wizard-app-hermes-agent', containerId: 'frontend', consumer: 'hermes-frontend-3' });
    expect(plan.connect).toEqual([]);
  });

  it('skips providers whose network does not exist yet', () => {
    const plan = planNetworkChanges(box, [grant('hermes-frontend-3', 'hermes-agent')], containers, new Set());
    expect(plan).toEqual({ connect: [], disconnect: [] });
  });
});
