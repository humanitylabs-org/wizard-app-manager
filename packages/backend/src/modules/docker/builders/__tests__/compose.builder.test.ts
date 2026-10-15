import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import yaml from 'yaml';
import { DockerComposeBuilder } from '../compose.builder';
import { createAppUrn } from '@/common/helpers/app-helpers';

const urn = createAppUrn('nginx', 'store-id');
const subnet = '10.128.1.0/24';

describe('DockerComposeBuilder', () => {
  let composeBuilder: DockerComposeBuilder;

  beforeEach(() => {
    composeBuilder = new DockerComposeBuilder();
  });

  describe('Variable interpolation', () => {
    it('should replace {{RUNTIPI_APP_ID}} in label values', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    labels:
      runtipi.app_id: '{{RUNTIPI_APP_ID}}'
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.labels['runtipi.app_id']).toBe('nginx-store-id');
    });

    it('should replace {{ RUNTIPI_APP_ID }} with spaces in label values', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    labels:
      runtipi.app_id: '{{ RUNTIPI_APP_ID }}'
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.labels['runtipi.app_id']).toBe('nginx-store-id');
    });

    it('should replace {{RUNTIPI_APP_ID}} in label keys', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    labels:
      '{{RUNTIPI_APP_ID}}': 'value'
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.labels['nginx-store-id']).toBe('value');
    });

    it('should replace multiple {{RUNTIPI_APP_ID}} occurrences', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    labels:
      test: '{{RUNTIPI_APP_ID}}-{{RUNTIPI_APP_ID}}'
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.labels.test).toBe('nginx-store-id-nginx-store-id');
    });

    it('should not replace non-string label values', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    labels:
      numeric_label: 123
      boolean_label: true
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.labels.numeric_label).toBe(123);
      expect(resultParsed.services.web.labels.boolean_label).toBe(true);
    });
  });

  describe('Architecture overrides', () => {
    it('should merge architecture-specific overrides', () => {
      const doc = `
services:
  web:
    image: nginx:alpine
x-runtipi:
  overrides:
    - architecture: arm64
      services:
        web:
          image: nginx:arm64
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet, 'arm64');

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.image).toBe('nginx:arm64');
    });

    it('should not merge overrides for different architecture', () => {
      const doc = `
services:
  web:
    image: nginx:alpine
x-runtipi:
  overrides:
    - architecture: arm64
      services:
        web:
          image: nginx:arm64
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet, 'amd64');

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.image).toBe('nginx:alpine');
    });
  });

  describe('Restart policy preservation', () => {
    it('should preserve restart policy from YAML', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    restart: always
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.restart).toBe('always');
    });

    it('should add default restart: unless-stopped if not specified', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.restart).toBe('unless-stopped');
    });
  });

  describe('Network configuration', () => {
    it('should add app-specific network for multi-service apps', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
  db:
    image: postgres
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.networks['nginx_store-id_network']).toBeDefined();
      expect(resultParsed.networks['nginx_store-id_network'].ipam).toBeDefined();
      expect(resultParsed.networks['nginx_store-id_network'].ipam.config[0].subnet).toBe(subnet);
    });

    it('should add tipi_main_network when is_main is true', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    x-runtipi:
      is_main: true
      internal_port: 80
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.networks.tipi_main_network).toBeDefined();
      expect(resultParsed.networks.tipi_main_network.name).toBe('runtipi_tipi_main_network');
      expect(resultParsed.networks.tipi_main_network.external).toBe(true);
    });

    it('should add tipi_main_network when add_to_main_network is true', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    x-runtipi:
      add_to_main_network: true
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.networks.tipi_main_network).toBeDefined();
    });
  });

  describe('Port configuration', () => {
    it('should add port when is_main and openPort are set', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    x-runtipi:
      is_main: true
      internal_port: 80
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.ports).toContain('${APP_PORT}:80');
    });

    it('should not add port when openPort is false', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    x-runtipi:
      is_main: true
      internal_port: 80
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: false, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.ports).toBeUndefined();
    });
  });

  describe('Traefik labels', () => {
    it('should add Traefik labels when exposed is true', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    x-runtipi:
      is_main: true
      internal_port: 80
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.labels).toBeDefined();
      expect(resultParsed.services.web.labels['runtipi.managed']).toBe('true');
      expect(resultParsed.services.web.labels['runtipi.appurn']).toBe(urn);
    });

    it('should add Traefik labels when exposedLocal is true', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    x-runtipi:
      is_main: true
      internal_port: 80
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: false, domain: 'hello.com', exposedLocal: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.labels).toBeDefined();
    });

    it('should not add Traefik labels when not exposed', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    x-runtipi:
      is_main: true
      internal_port: 80
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(
        parsed,
        { openPort: false, domain: 'hello.com', exposed: false, exposedLocal: false },
        urn,
        subnet,
      );

      const resultParsed = yaml.parse(result);
      // Should only have runtipi labels, not Traefik labels
      expect(resultParsed.services.web.labels['runtipi.managed']).toBe('true');
      expect(resultParsed.services.web.labels['runtipi.appurn']).toBe(urn);
      expect(resultParsed.services.web.labels['traefik.enable']).toBeUndefined();
    });
  });

  describe('x-runtipi metadata', () => {
    it('should remove x-runtipi from services', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    x-runtipi:
      is_main: true
      internal_port: 80
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web['x-runtipi']).toBeUndefined();
    });

    it('should remove x-runtipi from root', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed['x-runtipi']).toBeUndefined();
    });
  });

  describe('Complete compose file', () => {
    it('should build a complete docker-compose.yml', () => {
      const doc = `x-runtipi:
  schema-version: 2

services:
  web:
    image: nginx:alpine
    restart: always
    labels:
      test: '{{RUNTIPI_APP_ID}}'
    x-runtipi:
      is_main: true
      internal_port: 80
  db:
    image: postgres
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);

      // Check service configuration
      expect(resultParsed.services.web.image).toBe('nginx:alpine');
      expect(resultParsed.services.web.restart).toBe('always');
      expect(resultParsed.services.web.labels.test).toBe('nginx-store-id');
      expect(resultParsed.services.web.labels['runtipi.managed']).toBe('true');
      expect(resultParsed.services.web.labels['runtipi.appurn']).toBe(urn);
      expect(resultParsed.services.web['x-runtipi']).toBeUndefined();

      // Check networks
      expect(resultParsed.networks['nginx_store-id_network']).toBeDefined();
      expect(resultParsed.networks.tipi_main_network).toBeDefined();

      // Check root x-runtipi removed
      expect(resultParsed['x-runtipi']).toBeUndefined();
    });

    it('should preserve empty named volumes without rendering null', () => {
      const doc = `services:
  web:
    image: nginx:alpine
volumes:
  nginx_logs:
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      expect(result).toContain(`volumes:
  nginx_logs:
`);
      expect(result).not.toContain('nginx_logs: null');
    });
  });

  describe('Network mode exclusivity', () => {
    it('should not add networks when network_mode is set to host', () => {
      const doc = `services:
  web:
    image: nginx:alpine
    network_mode: host
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.networks).toEqual({});
    });

    it('should not add networks when network_mode is set to none', () => {
      const doc = `services:
  web:
    image: nginx:alpine
    network_mode: none
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.networks).toEqual({});
    });

    it('should add networks normally when network_mode is not set', () => {
      const doc = `services:
  web:
    image: nginx:alpine
  db:
    image: postgres
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.networks['nginx_store-id_network']).toBeDefined();
    });

    it('should NOT add internal app network for single-service apps', () => {
      const doc = `services:
  web:
    image: nginx:alpine
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.networks['nginx_store-id_network']).toBeUndefined();
      expect(resultParsed.networks['nginx_store-id_network']).toBeUndefined();
    });

    it('should still add tipi_main_network for single-service exposed apps', () => {
      const doc = `services:
  web:
    image: nginx:alpine
    x-runtipi:
      is_main: true
      internal_port: 80
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.networks.tipi_main_network).toBeDefined();
      expect(resultParsed.services.web.networks['nginx_store-id_network']).toBeUndefined();
    });

    it('should add internal app network for multi-service apps', () => {
      const doc = `services:
  web:
    image: nginx:alpine
  db:
    image: postgres
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.networks['nginx_store-id_network']).toBeDefined();
      expect(resultParsed.services.db.networks['nginx_store-id_network']).toBeDefined();
      expect(resultParsed.networks['nginx_store-id_network']).toBeDefined();
    });

    it('should clear ports when network_mode is set', () => {
      const doc = `services:
  web:
    image: nginx:alpine
    network_mode: host
    ports:
      - "8080:80"
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.ports).toEqual([]);
    });
  });

  describe('Default restart policy', () => {
    it('should add default restart: unless-stopped when no restart specified', () => {
      const doc = `services:
  web:
    image: nginx:alpine
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.restart).toBe('unless-stopped');
    });

    it('should preserve existing restart policy when specified', () => {
      const doc = `services:
  web:
    image: nginx:alpine
    restart: always
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.restart).toBe('always');
    });

    it('should preserve restart: no when explicitly set', () => {
      const doc = `services:
  web:
    image: nginx:alpine
    restart: "no"
`;

      const parsed = yaml.parse(doc);
      const result = composeBuilder.getDockerCompose(parsed, { openPort: true, domain: 'hello.com', exposed: true }, urn, subnet);

      const resultParsed = yaml.parse(result);
      expect(resultParsed.services.web.restart).toBe('no');
    });
  });

  describe('Wizard app isolation (WIZARD_APP_ISOLATION)', () => {
    const wizardUrn = createAppUrn('hermes-agent', 'wizardapps');
    const doc = `x-runtipi:
  schema_version: 2
services:
  hermes-agent:
    image: python:3.12-alpine
    x-runtipi:
      is_main: true
      internal_port: 9119
  hermes-agent-engine:
    image: hermes:latest
    x-runtipi:
      add_to_main_network: true
  db:
    image: postgres:16
`;

    afterEach(() => {
      vi.unstubAllEnvs();
    });

    it('is byte-identical to stock when the flag is off', () => {
      vi.stubEnv('WIZARD_APP_ISOLATION', '');
      const off = composeBuilder.getDockerCompose(yaml.parse(doc), {}, wizardUrn, subnet);
      vi.stubEnv('WIZARD_APP_ISOLATION', '0');
      expect(composeBuilder.getDockerCompose(yaml.parse(doc), {}, wizardUrn, subnet)).toBe(off);
      const parsed = yaml.parse(off);
      expect(parsed.services['hermes-agent'].networks.tipi_main_network).toEqual({ gw_priority: 1 });
      expect(parsed.networks['wizard-app-hermes-agent']).toBeUndefined();
    });

    it('moves main and add_to_main_network services to wizard-app-<id> when on', () => {
      vi.stubEnv('WIZARD_APP_ISOLATION', '1');
      const parsed = yaml.parse(composeBuilder.getDockerCompose(yaml.parse(doc), {}, wizardUrn, subnet));
      for (const svc of ['hermes-agent', 'hermes-agent-engine']) {
        expect(parsed.services[svc].networks.tipi_main_network).toBeUndefined();
        expect(parsed.services[svc].networks['wizard-app-hermes-agent']).toEqual({ gw_priority: 1 });
        expect(parsed.services[svc].networks['hermes-agent_wizardapps_network']).toEqual({ gw_priority: 0 });
      }
      expect(parsed.services.db.networks).toEqual({ 'hermes-agent_wizardapps_network': { gw_priority: 0 } });
      expect(parsed.networks['wizard-app-hermes-agent']).toEqual({ name: 'wizard-app-hermes-agent', external: true });
      expect(parsed.networks['hermes-agent_wizardapps_network']).toEqual({ ipam: { config: [{ subnet }] } });
    });

    it('keeps a Traefik-exposed main service on the shared network so the proxy can reach it', () => {
      vi.stubEnv('WIZARD_APP_ISOLATION', '1');
      const parsed = yaml.parse(
        composeBuilder.getDockerCompose(yaml.parse(doc), { exposedLocal: true, localSubdomain: 'hermes' }, wizardUrn, subnet),
      );
      expect(parsed.services['hermes-agent'].networks.tipi_main_network).toEqual({ gw_priority: 0 });
      expect(parsed.services['hermes-agent'].networks['wizard-app-hermes-agent']).toEqual({ gw_priority: 1 });
      expect(parsed.services['hermes-agent-engine'].networks.tipi_main_network).toBeUndefined();
    });

    it('leaves apps from other stores untouched when on', () => {
      vi.stubEnv('WIZARD_APP_ISOLATION', '1');
      const on = composeBuilder.getDockerCompose(yaml.parse(doc), {}, urn, subnet);
      vi.stubEnv('WIZARD_APP_ISOLATION', '');
      expect(on).toBe(composeBuilder.getDockerCompose(yaml.parse(doc), {}, urn, subnet));
    });
  });
});
