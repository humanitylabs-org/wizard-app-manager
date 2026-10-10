import { Link } from 'react-router';
import { getInstalledAppsOptions, getLinksOptions } from '@/api-client/@tanstack/react-query.gen';
import { EmptyPage } from '@/components/empty-page/empty-page';
import type { CustomLink } from '@/types/app.types';
import { useSuspenseQuery } from '@tanstack/react-query';
import { LinkTile } from '../components/link-tile/link-tile';
import { useTranslation } from 'react-i18next';
import { useDisclosure } from '@/lib/hooks/use-disclosure';
import { AddLinkDialog } from '../components/dialogs/add-link/add-link-dialog';
import '@/styles/app-grid.css';
import { useAppContext } from '@/context/app-context';
import { RestartAllButton, StartAllButton, StopAllButton } from '../components/batch-actions-dialog/batch-actions-dialog';

// Wizard shelves for installed apps, drawn like the Wizard UX launcher. Apps from other stores land in "Other".
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
  const { t } = useTranslation();

  const { installed } = apps;
  const { links: customLinks = [] } = links;

  const renderApp = ({ info, app, metadata }: (typeof installed)[number]) => {
    const versionIsIgnored = app.ignoredVersion === metadata.latestVersion;
    const updateAvailable = Number(app.version) < Number(metadata.latestVersion) && !versionIsIgnored;
    const [appName, storeId] = info.urn.split(':');

    return (
      <Link key={app.id} to={`/apps/${storeId}/${appName}`} className="wlapp" aria-label={info.name} title={info.short_desc}>
        <span className="ic">
          <img src={`/api/marketplace/apps/${info.urn}/image`} alt="" loading="lazy" />
          <i className={`dot ${app.status}`} />
          {app.pendingRestart ? <b className="badge-up">↻</b> : updateAvailable ? <b className="badge-up">↑</b> : null}
        </span>
        <span className="nm">{info.name}</span>
      </Link>
    );
  };

  const renderLink = (link: CustomLink) => (
    <Link key={link.id} to={link.url} target="_blank" className="col-sm-6 col-lg-4 app-link p-2 pt-0 pb-0 mb-0">
      <LinkTile key={link.id} link={link} />
    </Link>
  );

  if (installed.length === 0 && customLinks.length === 0) {
    return <EmptyPage title="MY_APPS_EMPTY_TITLE" subtitle="MY_APPS_EMPTY_SUBTITLE" redirectPath="/app-store" actionLabel="MY_APPS_EMPTY_ACTION" />;
  }

  return (
    <div className="wsheet" data-testid="apps-list">
      {SHELVES.map(([shelf, label]) => {
        const group = installed.filter((a) => a.info.available && shelfOf(a.info.categories) === shelf);
        if (!group.length) return null;
        return (
          <div key={shelf} className="lgroup">
            <h4>
              {t(label)} <span>{group.length}</span>
            </h4>
            <div className="lgrid">{group.map(renderApp)}</div>
          </div>
        );
      })}
      {customLinks.length ? <div className="row row-cards m-0">{customLinks.map(renderLink)}</div> : null}
      {installed.length > 1 ? (
        <div className="wbatch">
          <StartAllButton availableUpdates={updatesAvailable} />
          <StopAllButton availableUpdates={updatesAvailable} />
          <RestartAllButton availableUpdates={updatesAvailable} />
        </div>
      ) : null}
      <AddLinkDialog isOpen={addLinkDisclosure.isOpen} onClose={() => addLinkDisclosure.close()} />
    </div>
  );
};
