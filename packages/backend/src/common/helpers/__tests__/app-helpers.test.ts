import type { AppUrn } from '@runtipi/common/types';
import { castAppUrn, createAppUrn, extractAppUrn, validateAppUrn } from '../app-helpers';
import { describe, expect, it } from 'vitest';

describe('validateAppUrn', () => {
  it('should accept valid URNs with alphanumeric characters', () => {
    expect(validateAppUrn('test-app:my-store')).toBe('test-app:my-store');
    expect(validateAppUrn('app_name:store_id')).toBe('app_name:store_id');
    expect(validateAppUrn('App123:Store456')).toBe('App123:Store456');
    expect(validateAppUrn('APP:STORE')).toBe('APP:STORE');
  });

  it('should accept valid URNs with hyphens and underscores', () => {
    expect(validateAppUrn('my-app-name:my-store-name')).toBe('my-app-name:my-store-name');
    expect(validateAppUrn('my_app_name:my_store_name')).toBe('my_app_name:my_store_name');
    expect(validateAppUrn('app_123-name:store_456-name')).toBe('app_123-name:store_456-name');
  });

  it('should reject URNs with path traversal sequences', () => {
    expect(() => validateAppUrn('../app:store')).toThrow('Invalid app name: ../app');
    expect(() => validateAppUrn('app:../../../etc')).toThrow('Invalid app store id: ../../../etc');
    expect(() => validateAppUrn('..:..')).toThrow('Invalid app name: ..');
  });

  it('should reject URNs with special characters', () => {
    expect(() => validateAppUrn('app.name:store')).toThrow('Invalid app name: app.name');
    expect(() => validateAppUrn('app:store;rm')).toThrow('Invalid app store id: store;rm');
    expect(() => validateAppUrn('app:store$(whoami)')).toThrow('Invalid app store id: store$(whoami)');
    expect(() => validateAppUrn('app:store`whoami`')).toThrow('Invalid app store id: store`whoami`');
    expect(() => validateAppUrn('app:store|cat')).toThrow('Invalid app store id: store|cat');
    expect(() => validateAppUrn('app:store&ls')).toThrow('Invalid app store id: store&ls');
  });

  it('should reject URNs with slashes', () => {
    expect(() => validateAppUrn('app/name:store')).toThrow('Invalid app name: app/name');
    expect(() => validateAppUrn('app:store/name')).toThrow('Invalid app store id: store/name');
    expect(() => validateAppUrn('app\\name:store')).toThrow('Invalid app name: app\\name');
  });

  it('should reject URNs with spaces', () => {
    expect(() => validateAppUrn('app name:store')).toThrow('Invalid app name: app name');
    expect(() => validateAppUrn('app:store name')).toThrow('Invalid app store id: store name');
  });

  it('should reject URNs without colon separator', () => {
    expect(() => validateAppUrn('appname')).toThrow('Invalid namespaced app id: appname');
    expect(() => validateAppUrn('')).toThrow('Invalid namespaced app id: ');
  });

  it('should reject URNs with empty app name or store id', () => {
    expect(() => validateAppUrn(':store')).toThrow('Invalid app name');
    expect(() => validateAppUrn('app:')).toThrow('Invalid app store id');
    expect(() => validateAppUrn(':')).toThrow('Invalid app name');
  });

  it('should reject URNs with null bytes', () => {
    expect(() => validateAppUrn('app\x00:store')).toThrow();
  });
});

describe('extractAppUrn', () => {
  it('should extract app name and store id from valid URN', () => {
    const result = extractAppUrn('test-app:my-store' as AppUrn);
    expect(result.appName).toBe('test-app');
    expect(result.appStoreId).toBe('my-store');
  });

  it('should reject invalid URNs', () => {
    expect(() => extractAppUrn('../app:store' as AppUrn)).toThrow();
    expect(() => extractAppUrn('app.name:store' as AppUrn)).toThrow();
    expect(() => extractAppUrn('app' as AppUrn)).toThrow();
  });

  it('should extract URNs with underscores', () => {
    const result = extractAppUrn('my_app_name:my_store_id' as AppUrn);
    expect(result.appName).toBe('my_app_name');
    expect(result.appStoreId).toBe('my_store_id');
  });
});

describe('castAppUrn', () => {
  it('should cast valid string to AppUrn', () => {
    expect(castAppUrn('test-app:my-store')).toBe('test-app:my-store');
    expect(castAppUrn('app_name:store_id')).toBe('app_name:store_id');
  });

  it('should reject invalid URNs', () => {
    expect(() => castAppUrn('../app:store')).toThrow();
    expect(() => castAppUrn('app.name:store')).toThrow();
    expect(() => castAppUrn('app:store/path')).toThrow();
    expect(() => castAppUrn('app:store;rm')).toThrow();
  });
});

describe('createAppUrn', () => {
  it('should create valid URN from app name and store', () => {
    expect(createAppUrn('test-app', 'my-store')).toBe('test-app:my-store');
    expect(createAppUrn('app_name', 'store_id')).toBe('app_name:store_id');
  });

  it('should create URN that passes validation', () => {
    const urn = createAppUrn('test-app', 'my-store');
    expect(() => validateAppUrn(urn)).not.toThrow();
  });

  it('should create URN that can be extracted', () => {
    const urn = createAppUrn('test-app', 'my-store');
    const result = extractAppUrn(urn);
    expect(result.appName).toBe('test-app');
    expect(result.appStoreId).toBe('my-store');
  });
});

describe('Security Tests', () => {
  it('should prevent path traversal via URN', () => {
    const maliciousUrns = [
      '../../../etc:passwd',
      'app:../../../etc',
      '..:app',
      'app:..',
      './app:store',
      'app:./store',
      '/etc/passwd:app',
      'app:/etc/passwd',
    ];

    maliciousUrns.forEach((urn) => {
      expect(() => validateAppUrn(urn)).toThrow();
    });
  });

  it('should prevent command injection via URN', () => {
    const maliciousUrns = [
      'app:store;rm -rf /',
      'app:store|cat /etc/passwd',
      'app:store$(whoami)',
      'app:store`whoami`',
      'app:store&&ls',
      'app:store||ls',
    ];

    maliciousUrns.forEach((urn) => {
      expect(() => validateAppUrn(urn)).toThrow();
    });
  });

  it('should prevent null byte injection via URN', () => {
    const maliciousUrns = ['app\x00:store', 'app:store\x00', 'app\x00\x00:store'];

    maliciousUrns.forEach((urn) => {
      expect(() => validateAppUrn(urn)).toThrow();
    });
  });

  it('should prevent newline injection via URN', () => {
    const maliciousUrns = ['app\n:store', 'app:store\n', 'app\r\n:store'];

    maliciousUrns.forEach((urn) => {
      expect(() => validateAppUrn(urn)).toThrow();
    });
  });
});
