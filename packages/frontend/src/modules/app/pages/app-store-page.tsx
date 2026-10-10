import { getEnabledAppStoresOptions, getInstalledAppsOptions, searchAppsInfiniteOptions } from '@/api-client/@tanstack/react-query.gen';
import { EmptyPage } from '@/components/empty-page/empty-page';
import { useInfiniteScroll } from '@/lib/hooks/use-infinite-scroll';
import { useAppStoreState } from '@/stores/app-store';
import { keepPreviousData, useInfiniteQuery, useQuery, useSuspenseQuery } from '@tanstack/react-query';
import { Navigate, useParams, useSearchParams } from 'react-router';
import { StoreTile } from '../components/store-tile/store-tile';
import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { StoreSelector } from '@/components/store-selector/store-selector';
import { IconSearch } from '@tabler/icons-react';
import { AppGraph, type GraphNode } from '@/components/app-graph/app-graph';
import { STORE_SLUG, nodeFromIndex, searchIndex } from '@/lib/wizard-index';

export const AppStorePageSuspense = () => {
  return <div className="card px-3 pb-3" style={{ height: 4000 }} />;
};

const useDebounced = <T,>(value: T, ms: number) => {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
};

// The App Store draws the Wizard App Index as the same graph as My Apps: interfaces next to the app they open,
// the system apps each one uses, and Screen / Headless on every node. Search replaces the category dropdown.
export default () => {
  const params = useParams<{ storeId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedStore = searchParams.get('store') ?? undefined;
  const { storeId, setStoreId, search: initialSearch, setSearch } = useAppStoreState();
  const [search, setLocalSearch] = useState(initialSearch);
  const q = useDebounced(search.trim(), 250);
  const { t } = useTranslation();

  useEffect(() => {
    if (selectedStore !== storeId) setStoreId(selectedStore);
  }, [selectedStore, setStoreId, storeId]);

  const { data: appStores } = useSuspenseQuery({ ...getEnabledAppStoresOptions() });
  const { data: mine } = useQuery({ ...getInstalledAppsOptions() });

  const wizardStore = appStores.appStores.find((s) => s.slug === STORE_SLUG);
  const showGraph = Boolean(wizardStore) && (!storeId || storeId === STORE_SLUG);

  const onSearch = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setLocalSearch(e.target.value);
      setSearch(e.target.value);
    },
    [setSearch],
  );
  const onSelectStore = useCallback(
    (value?: string) => {
      setSearchParams(value ? { store: value } : {});
      setStoreId(value);
    },
    [setSearchParams, setStoreId],
  );

  // Wizard store: read the index directly (all apps for wiring, matches for the current search).
  const all = useQuery({ queryKey: ['wizard-index', wizardStore?.url, ''], queryFn: () => searchIndex(wizardStore?.url, ''), enabled: showGraph, staleTime: 60_000 });
  const hits = useQuery({
    queryKey: ['wizard-index', wizardStore?.url, q],
    queryFn: () => searchIndex(wizardStore?.url, q),
    enabled: showGraph && q.length > 0,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });

  // Other stores keep Runtipi's list.
  const list = useInfiniteQuery({
    ...searchAppsInfiniteOptions({ query: { search: q, pageSize: 24, storeId } }),
    getNextPageParam: (lastPage) => lastPage.nextCursor,
    placeholderData: keepPreviousData,
    enabled: !showGraph,
  });
  const { lastElementRef } = useInfiniteScroll({
    fetchNextPage: list.fetchNextPage,
    hasNextPage: Boolean(list.hasNextPage),
    isFetching: list.isFetchingNextPage || list.isFetching,
  });

  if (params.storeId) return <Navigate to={`/app-store?store=${params.storeId}`} />;

  const installed = new Map((mine?.installed ?? []).map((a) => [a.info.urn, a.app.status]));
  const toNode = (a: Parameters<typeof nodeFromIndex>[0]): GraphNode =>
    nodeFromIndex(a, `/app-store/${STORE_SLUG}/${a.id}`, installed.has(`${a.id}:${STORE_SLUG}`) ? { installed: true } : { cta: 'Install' });

  const bar = (
    <div className="wstorebar">
      <div className="wsearch">
        <IconSearch size={18} />
        <input value={search} onChange={onSearch} placeholder={t('APP_STORE_SEARCH_PLACEHOLDER')} aria-label={t('APP_STORE_SEARCH_PLACEHOLDER')} />
      </div>
      {appStores.appStores.length > 1 ? <StoreSelector initialValue={selectedStore} stores={appStores.appStores} onSelect={onSelectStore} /> : null}
    </div>
  );

  if (showGraph) {
    if (all.isLoading) return <AppStorePageSuspense />;
    if (all.isError) return <>{bar}<EmptyPage title="APP_STORE_NO_RESULTS" subtitle="APP_STORE_NO_RESULTS_SUBTITLE" /></>;
    const pool = (all.data ?? []).map(toNode);
    const shown = q ? (hits.data ?? []).map(toNode) : pool;
    return (
      <>
        {bar}
        {shown.length ? (
          <AppGraph nodes={shown} pool={pool} missingHref={(id) => `/app-store/${STORE_SLUG}/${id}`} ghostLabel="Not in the store" />
        ) : (
          <EmptyPage title="APP_STORE_NO_RESULTS" subtitle="APP_STORE_NO_RESULTS_SUBTITLE" />
        )}
      </>
    );
  }

  const apps = list.data?.pages.flatMap((page) => page.data) ?? [];
  if (!list.data) return <AppStorePageSuspense />;
  return (
    <>
      {bar}
      {apps.length ? (
        <div className="wsheet wstore">
          <div className="lgroup">
            <h4>
              {t('APP_STORE_ALL_APPS', { defaultValue: 'All apps' })} <span>{apps.length}</span>
            </h4>
            {apps.map((app, index) => (
              <div ref={index === apps.length - 1 ? lastElementRef : null} key={app.urn}>
                <StoreTile app={app} isLoading={false} />
              </div>
            ))}
          </div>
        </div>
      ) : (
        <EmptyPage title="APP_STORE_NO_RESULTS" subtitle="APP_STORE_NO_RESULTS_SUBTITLE" />
      )}
    </>
  );
};
