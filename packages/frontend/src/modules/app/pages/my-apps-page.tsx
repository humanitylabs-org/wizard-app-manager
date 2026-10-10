import { Link } from 'react-router';
import { getEnabledAppStoresOptions, getInstalledAppsOptions, getLinksOptions } from '@/api-client/@tanstack/react-query.gen';
import { EmptyPage } from '@/components/empty-page/empty-page';
import type { CustomLink } from '@/types/app.types';
import { useQuery, useSuspenseQuery } from '@tanstack/react-query';
import { LinkTile } from '../components/link-tile/link-tile';
import '@/styles/app-grid.css';
import { useAppContext } from '@/context/app-context';
import { RestartAllButton, StartAllButton, StopAllButton } from '../components/batch-actions-dialog/batch-actions-dialog';
import { AppGraph, type GraphNode } from '@/components/app-graph/app-graph';
import { STORE_SLUG, hasScreen, kindOf, lookupIndex } from '@/lib/wizard-index';

// My Apps as a graph: each agent/app with the interfaces that open it and the system apps it uses.
// Live status and updates come from the App Manager; how apps connect (uses, type) comes from the index.
export default () => {
  const { data: apps } = useSuspenseQuery({ ...getInstalledAppsOptions() });
  const { data: links } = useSuspenseQuery({ ...getLinksOptions() });
  const { data: stores } = useQuery({ ...getEnabledAppStoresOptions() });
  const { updatesAvailable } = useAppContext();

  const { installed } = apps;
  const { links: customLinks = [] } = links;
  const indexUrl = stores?.appStores.find((s) => s.slug === STORE_SLUG)?.url;

  const wizardIds = installed.filter((a) => a.info.urn.endsWith(`:${STORE_SLUG}`)).map((a) => a.info.id);
  const { data: meta = [] } = useQuery({
    queryKey: ['wizard-index-meta', indexUrl, [...wizardIds].sort().join(',')],
    queryFn: () => lookupIndex(indexUrl, wizardIds),
    enabled: wizardIds.length > 0,
    staleTime: 5 * 60 * 1000,
  });
  const metaById = new Map(meta.map((m) => [m.id, m]));

  const nodes: GraphNode[] = installed
    .filter((a) => a.info.available)
    .map(({ info, app, metadata }) => {
      const [appName = info.id, storeId = ''] = info.urn.split(':');
      const m = storeId === STORE_SLUG ? metaById.get(appName) : undefined;
      const cats = (info.categories ?? []) as string[];
      const kind = storeId === STORE_SLUG ? kindOf(m?.category ?? cats[0], m?.type) : 'other';
      const versionIsIgnored = app.ignoredVersion === metadata.latestVersion;
      return {
        id: appName,
        name: info.name,
        desc: m?.summary ?? info.short_desc,
        kind,
        screen: m ? hasScreen(m.type) : !info.no_gui,
        uses: m?.uses ?? [],
        href: `/apps/${storeId}/${appName}`,
        logo: `/api/marketplace/apps/${info.urn}/image`,
        status: app.status,
        update: Number(app.version) < Number(metadata.latestVersion) && !versionIsIgnored,
      };
    });

  const renderLink = (link: CustomLink) => (
    <Link key={link.id} to={link.url} target="_blank" className="col-sm-6 col-lg-4 app-link p-2 pt-0 pb-0 mb-0">
      <LinkTile key={link.id} link={link} />
    </Link>
  );

  if (installed.length === 0 && customLinks.length === 0) {
    return <EmptyPage title="MY_APPS_EMPTY_TITLE" subtitle="MY_APPS_EMPTY_SUBTITLE" redirectPath="/app-store" actionLabel="MY_APPS_EMPTY_ACTION" />;
  }

  return (
    <div data-testid="apps-list">
      <AppGraph
        nodes={nodes}
        animate
        missingHref={(id) => `/app-store/${STORE_SLUG}/${id}`}
        ghostLabel="Not installed"
        footer={
          <>
            {customLinks.length ? <div className="row row-cards m-0">{customLinks.map(renderLink)}</div> : null}
            {installed.length > 1 ? (
              <div className="wbatch">
                <StartAllButton availableUpdates={updatesAvailable} />
                <StopAllButton availableUpdates={updatesAvailable} />
                <RestartAllButton availableUpdates={updatesAvailable} />
              </div>
            ) : null}
          </>
        }
      />
    </div>
  );
};
