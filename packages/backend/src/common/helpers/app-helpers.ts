import type { AppUrn } from '@runtipi/common/types';

export const validateAppUrn = (urn: string): AppUrn => {
  const separatorIndex = urn.indexOf(':');
  if (separatorIndex === -1) {
    throw new Error(`Invalid namespaced app id: ${urn}`);
  }

  const appName = urn.substring(0, separatorIndex);
  const appStoreId = urn.substring(separatorIndex + 1);

  const allowedCharsRegex = /^[a-zA-Z0-9_-]+$/;

  if (!allowedCharsRegex.test(appName)) {
    throw new Error(`Invalid app name: ${appName}. Only alphanumeric, hyphens, and underscores allowed.`);
  }

  if (!allowedCharsRegex.test(appStoreId)) {
    throw new Error(`Invalid app store id: ${appStoreId}. Only alphanumeric, hyphens, and underscores allowed.`);
  }

  if (!appName || !appStoreId) {
    throw new Error(`Invalid App URN: ${urn}`);
  }

  return urn as AppUrn;
};

export const extractAppUrn = (id: AppUrn) => {
  const validated = validateAppUrn(id);
  const separatorIndex = validated.indexOf(':');
  const appName = validated.substring(0, separatorIndex);
  const appStoreId = validated.substring(separatorIndex + 1);

  return { appName, appStoreId };
};

export const createAppUrn = (appName: string, appstore: string) => {
  return `${appName}:${appstore}` as AppUrn;
};

export const castAppUrn = (id: string): AppUrn => {
  return validateAppUrn(id);
};
