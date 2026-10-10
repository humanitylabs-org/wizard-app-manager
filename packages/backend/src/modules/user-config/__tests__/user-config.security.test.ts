import { mock } from 'vitest-mock-extended';
import { beforeAll, describe, expect, it } from 'vitest';
import { UserConfigController } from '../user-config.controller';
import { UserConfigService } from '../user-config.service';
import { fromPartial } from '@total-typescript/shoehorn';

describe('UserConfigController Security', () => {
  let userConfigController: UserConfigController;
  let userConfigService = mock<UserConfigService>();

  beforeAll(async () => {
    userConfigService = mock<UserConfigService>();
    userConfigController = new UserConfigController(userConfigService);
  });

  describe('URN Validation', () => {
    it('should throw error for URN with path traversal in app name', async () => {
      await expect(userConfigController.getUserConfig('../../etc/passwd:store' as const)).rejects.toThrow();
    });

    it('should throw error for URN with path traversal in store id', async () => {
      await expect(userConfigController.getUserConfig('app:../../../etc' as const)).rejects.toThrow();
    });

    it('should throw error for URN with special characters', async () => {
      await expect(userConfigController.getUserConfig('app.name:store' as const)).rejects.toThrow();
    });

    it('should throw error for URN with command injection characters', async () => {
      await expect(userConfigController.getUserConfig('app:store;rm' as const)).rejects.toThrow();
    });

    it('should throw error for URN with spaces', async () => {
      await expect(userConfigController.getUserConfig('app name:store' as const)).rejects.toThrow();
    });

    it('should accept valid URN', async () => {
      userConfigService.getUserConfig.mockResolvedValue(
        fromPartial({
          dockerCompose: 'version: "3.9"',
          appEnv: 'KEY=value',
          sourceCompose: 'version: "3.9"',
          isEnabled: true,
        }),
      );

      const result = await userConfigController.getUserConfig('test-app:my-store' as const);

      expect(result.dockerCompose).toBe('version: "3.9"');
    });

    it('should accept URN with hyphens', async () => {
      userConfigService.getUserConfig.mockResolvedValue(
        fromPartial({
          dockerCompose: 'version: "3.9"',
          appEnv: 'KEY=value',
          sourceCompose: 'version: "3.9"',
          isEnabled: true,
        }),
      );

      const result = await userConfigController.getUserConfig('test-app-name:my-store-id' as const);

      expect(result.dockerCompose).toBe('version: "3.9"');
    });

    it('should accept URN with underscores', async () => {
      userConfigService.getUserConfig.mockResolvedValue(
        fromPartial({
          dockerCompose: 'version: "3.9"',
          appEnv: 'KEY=value',
          sourceCompose: 'version: "3.9"',
          isEnabled: true,
        }),
      );

      const result = await userConfigController.getUserConfig('test_app_name:my_store_id' as const);

      expect(result.dockerCompose).toBe('version: "3.9"');
    });
  });

  describe('Combined Attack Scenarios', () => {
    it('should prevent path traversal to write outside allowed directories', async () => {
      const maliciousUrns = [
        '../../../etc/passwd:store' as const,
        'app:../../../etc/passwd' as const,
        '..:app' as const,
        'app:..' as const,
        '/etc/passwd:app' as const,
        'app:/etc/passwd' as const,
      ];

      for (const urn of maliciousUrns) {
        await expect(userConfigController.getUserConfig(urn)).rejects.toThrow();
      }
    });

    it('should prevent command injection via URN', async () => {
      const maliciousUrns = [
        'app:store;rm -rf /' as const,
        'app:store|cat /etc/passwd' as const,
        'app:store$(whoami)' as const,
        'app:store`whoami`' as const,
        'app:store&&ls' as const,
      ];

      for (const urn of maliciousUrns) {
        await expect(userConfigController.getUserConfig(urn)).rejects.toThrow();
      }
    });

    it('should prevent null byte injection via URN', async () => {
      const maliciousUrns = ['app\x00:store' as const, 'app:store\x00' as const];

      for (const urn of maliciousUrns) {
        await expect(userConfigController.getUserConfig(urn)).rejects.toThrow();
      }
    });

    it('should prevent newline injection via URN', async () => {
      const maliciousUrns = ['app\n:store' as const, 'app:store\n' as const];

      for (const urn of maliciousUrns) {
        await expect(userConfigController.getUserConfig(urn)).rejects.toThrow();
      }
    });
  });
});
