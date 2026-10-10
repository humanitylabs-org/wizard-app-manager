import { Link, useNavigate } from 'react-router';
import { getInstalledAppsOptions, getLinksOptions } from '@/api-client/@tanstack/react-query.gen';
import { EmptyPage } from '@/components/empty-page/empty-page';
import type { CustomLink } from '@/types/app.types';
import { useSuspenseQuery } from '@tanstack/react-query';
import { AppTile } from '../components/app-tile/app-tile';
import { LinkTile } from '../components/link-tile/link-tile';
import { ButtonTile } from '../components/button-tile/button-tile';
import { IconLayoutGridAdd, IconLinkPlus } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';
import { useDisclosure } from '@/lib/hooks/use-disclosure';
import { AddLinkDialog } from '../components/dialogs/add-link/add-link-dialog';
import '@/styles/app-grid.css';
import { useAppContext } from '@/context/app-context';
import { ActionBar } from '@/components/action-bar/action-bar';
import clsx from 'clsx';
import { RestartAllButton, StartAllButton, StopAllButton } from '../components/batch-actions-dialog/batch-actions-dialog';

// Wizard shelves for installed apps. Apps from other stores (Runtipi categories) land in "Other".
const SHELVES = [
  ['agent', 'APP_CATEGORY_AGENT'],
  ['interface', 'APP_CATEGORY_INTERFACE'],
  ['app', 'APP_CATEGORY_APP'],
  ['system', 'APP_CATEGORY_SYSTEM'],
  ['other', 'MY_APPS_OTHER'],
] as const;

const shelfOf = (categories?: readonly string[]) => {
  const c = categories ?? [];
  if (c.includes('agent')) return 'agent';
  if (c.includes('interface')) return 'interface';
  if (c.includes('system')) return 'system';
  if (c.includes('app')) return 'app';
  return 'other';
};

export default () => {
  const { data: apps } = useSuspenseQuery({
    ...getInstalledAppsOptions(),
  });

  const { data: links } = useSuspenseQuery({
    ...getLinksOptions(),
  });

  const { updatesAvailable } = useAppContext();

  const addLinkDisclosure = useDisclosure();
  const navigate = useNavigate();
  const { t } = useTranslation();

  const { installed } = apps;
  const { links: customLinks = [] } = links;

  const renderApp = ({ info, app, metadata }: (typeof installed)[number]) => {
    const versionIsIgnored = app.ignoredVersion === metadata.latestVersion;
    const updateAvailable = Number(app.version) < Number(metadata.latestVersion) && !versionIsIgnored;

    const [appName, storeId] = info.urn.split(':');

    if (info.available) {
      return (
        <Link key={app.id} to={`/apps/${storeId}/${appName}`} className="col-sm-6 col-lg-4 app-link p-2 pt-0 pb-0 mb-0">
          <AppTile key={info.urn} status={app.status} info={info} updateAvailable={updateAvailable} pendingRestart={app.pendingRestart} />
        </Link>
      );
    }

    return null;
  };

  const renderLink = (link: CustomLink) => {
    return (
      <Link key={link.id} to={link.url} target="_blank" className="col-sm-6 col-lg-4 app-link p-2 pt-0 pb-0 mb-0">
        <LinkTile key={link.id} link={link} />
      </Link>
    );
  };

  return (
    <>
      <ActionBar className={clsx({ 'd-none': installed.length <= 1 })}>
        <ActionBar.Left>
          <StartAllButton availableUpdates={updatesAvailable} />
        </ActionBar.Left>
        <ActionBar.Center>
          <StopAllButton availableUpdates={updatesAvailable} />
        </ActionBar.Center>
        <ActionBar.Right>
          <RestartAllButton availableUpdates={updatesAvailable} />
        </ActionBar.Right>
      </ActionBar>
      {installed.length === 0 && customLinks.length === 0 ? (
        <EmptyPage
          title="MY_APPS_EMPTY_TITLE"
          subtitle="MY_APPS_EMPTY_SUBTITLE"
          redirectPath="/app-store"
          actionLabel="MY_APPS_EMPTY_ACTION"
          extraContent={
            <div className="d-flex flex-column flex-sm-row gap-2 justify-content-center">
              <ButtonTile
                title={t('CUSTOM_APP_ADD_TITLE')}
                subtitle={t('CUSTOM_APP_ADD_SUBTITLE')}
                action={() => navigate('/apps/create')}
                icon={<IconLayoutGridAdd size={50} stroke={1.5} color="#A4A4A4" />}
                className="col-12 col-sm-6 col-lg-6 col-lg-6"
              />
              <ButtonTile
                title={t('LINKS_ADD_TITLE')}
                subtitle={t('LINKS_ADD_SUBTITLE')}
                action={() => addLinkDisclosure.open()}
                icon={<IconLinkPlus size={50} stroke={1.5} color="#A4A4A4" />}
                className="col-12 col-sm-6 col-md-6 col-lg-6"
              />
            </div>
          }
        />
      ) : (
        <div className="row row-cards" data-testid="apps-list">
          {SHELVES.map(([shelf, label]) => {
            const group = installed.filter((a) => shelfOf(a.info.categories) === shelf);
            if (!group.length) return null;
            return (
              <div key={shelf} className="wizard-shelf col-12 p-0">
                <h2 className="wizard-shelf-title px-2 mt-3 mb-2">{t(label)}</h2>
                <div className="row row-cards m-0">{group.map(renderApp)}</div>
              </div>
            );
          })}
          {customLinks.map(renderLink)}
          <ButtonTile
            title={t('CUSTOM_APP_ADD_TITLE')}
            subtitle={t('CUSTOM_APP_ADD_SUBTITLE')}
            action={() => navigate('/apps/create')}
            icon={<IconLayoutGridAdd size={50} stroke={1.5} />}
          />
          <ButtonTile
            title={t('LINKS_ADD_TITLE')}
            subtitle={t('LINKS_ADD_SUBTITLE')}
            action={() => addLinkDisclosure.open()}
            icon={<IconLinkPlus size={50} stroke={1.5} />}
          />
        </div>
      )}
      <AddLinkDialog isOpen={addLinkDisclosure.isOpen} onClose={() => addLinkDisclosure.close()} />
    </>
  );
};
