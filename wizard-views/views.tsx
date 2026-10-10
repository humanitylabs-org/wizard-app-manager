/*
 * The App Manager's three views, built from Wizard UX pieces so they drop into the Wizard OS shell:
 *
 *   MyAppsView     -> the launcher sheet (swipe up): installed apps as tiles, each interface joined to the agent or app
 *                     it opens, headless apps with a dashed ring, search at the bottom (installed first, then the store)
 *   AppStoreView   -> the same sheet for everything in the Wizard App Index, installed ones ticked
 *   ResourcesView  -> a group for the Settings sheet: disk, CPU and memory rows, plus start/stop/restart all
 *
 * Markup and class names follow wizard-ux web/styles.css (.lgroup .lgrid .lapp .lrow .search .st-group .st-row),
 * see ./wux.css and ./README.md. Data: the App Manager API (installed apps, status, system load) and the public
 * Wizard App Index (names, logos, type, `uses` for pairing).
 */
import { getEnabledAppStoresOptions, getInstalledAppsOptions, systemLoadOptions } from '@/api-client/@tanstack/react-query.gen';
import { type IndexApp, STORE_SLUG, hasScreen, kindOf, lookupIndex, searchIndex } from '@/lib/wizard-index';
import { RestartAllButton, StartAllButton, StopAllButton } from '@/modules/app/components/batch-actions-dialog/batch-actions-dialog';
import { useAppContext } from '@/context/app-context';
import { keepPreviousData, useQuery, useSuspenseQuery } from '@tanstack/react-query';
import clsx from 'clsx';
import { type ReactNode, useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { IconCircuitResistor, IconCpu, IconDatabase } from '@tabler/icons-react';
import './wux.css';

type Kind = 'agent' | 'interface' | 'app' | 'system' | 'other';

export type Tile = {
  id: string;
  name: string;
  desc?: string;
  kind: Kind;
  screen: boolean;
  uses: string[];
  href: string;
  logo: string;
  status?: string;
  update?: boolean;
  installed?: boolean;
};

const GROUPS: [Kind | 'apps', string][] = [
  ['agent', 'Agents'],
  ['apps', 'Apps'],
  ['system', 'System'],
  ['other', 'Other'],
];

const useDebounced = <T,>(value: T, ms: number) => {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
};

const useIndexUrl = () => {
  const { data } = useQuery({ ...getEnabledAppStoresOptions() });
  return data?.appStores.find((s) => s.slug === STORE_SLUG)?.url;
};

const SearchIcon = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
);

/* ---- pieces ------------------------------------------------------------------------------------------------ */

export const Sheet = ({ children, search, top }: { children: ReactNode; search?: ReactNode; top?: boolean }) => (
  <div className="wux">
    <div className="wux-sheet">
      <div className="sheet-grip" />
      <div className={clsx('wux-body', { top })}>{children}</div>
      {search}
    </div>
  </div>
);

export const SearchBar = ({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder: string }) => (
  <form className="search" onSubmit={(e) => e.preventDefault()}>
    <SearchIcon />
    <input
      type="search"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={placeholder}
      spellCheck={false}
    />
  </form>
);

export const AppTile = ({ t }: { t: Tile }) => (
  <Link to={t.href} className={clsx('lapp', { headless: !t.screen })} title={t.desc} aria-label={t.name}>
    <span className="ic">
      <img src={t.logo} alt="" loading="lazy" />
      {t.status ? <i className={`dot ${t.status}`} /> : null}
      {t.update ? <b className="up">↑</b> : null}
      {t.installed && !t.status ? <b className="ok">✓</b> : null}
    </span>
    <span className="nm">{t.name}</span>
  </Link>
);

const KIND_LABEL: Record<Kind, string> = { agent: 'Agent', interface: 'Interface', app: 'App', system: 'System', other: 'App' };

export const AppRow = ({ t, tag }: { t: Tile; tag?: string }) => (
  <Link to={t.href} className="lrow" aria-label={t.name}>
    <span className="ic">
      <img src={t.logo} alt="" loading="lazy" />
    </span>
    <span className="nm">
      {t.name}
      <small>{t.desc}</small>
    </span>
    <span className={clsx('kind', t.installed ? 'installed' : t.kind)}>{tag ?? (t.installed ? 'Installed' : KIND_LABEL[t.kind])}</span>
  </Link>
);

/** Units for the grid: each agent or app with the interfaces that open it (interface first, joined by a wire). */
export const arrange = (tiles: Tile[]) => {
  const paired = new Set<string>();
  const unit = (p: Tile) => {
    const ins = tiles.filter((i) => i.kind === 'interface' && i.uses.includes(p.id));
    for (const i of ins) paired.add(i.id);
    return [...ins, p];
  };
  const agent = tiles.filter((t) => t.kind === 'agent').map(unit);
  const app = tiles.filter((t) => t.kind === 'app').map(unit);
  const loose = tiles.filter((t) => t.kind === 'interface' && !paired.has(t.id)).map((t) => [t]);
  return {
    agent,
    apps: [...app, ...loose],
    system: tiles.filter((t) => t.kind === 'system').map((t) => [t]),
    other: tiles.filter((t) => t.kind === 'other').map((t) => [t]),
  } as Record<Kind | 'apps', Tile[][]>;
};

export const TileGroups = ({ tiles, extra }: { tiles: Tile[]; extra?: Partial<Record<Kind | 'apps', ReactNode>> }) => {
  const units = arrange(tiles);
  return (
    <>
      {GROUPS.map(([key, title]) => {
        const list = units[key] ?? [];
        if (!list.length && !extra?.[key]) return null;
        return (
          <div className="lgroup" key={key}>
            <h4>
              {title} <span>{list.reduce((n, u) => n + u.length, 0) || ''}</span>
            </h4>
            <div className="lgrid">
              {list.map((u) =>
                u.length > 1 ? (
                  <div key={u.map((t) => t.id).join('+')} className={clsx('lpair', `n${Math.min(u.length, 4)}`)}>
                    {u.map((t) => (
                      <AppTile key={t.id} t={t} />
                    ))}
                  </div>
                ) : (
                  u[0] && <AppTile key={u[0].id} t={u[0]} />
                ),
              )}
              {extra?.[key]}
            </div>
          </div>
        );
      })}
    </>
  );
};

const tileFromIndex = (a: IndexApp, installed: boolean): Tile => ({
  id: a.id,
  name: a.name,
  desc: a.summary,
  kind: kindOf(a.category, a.type),
  screen: hasScreen(a.type),
  uses: a.uses ?? [],
  href: `/app-store/${STORE_SLUG}/${a.id}`,
  logo: `/api/marketplace/apps/${a.id}:${STORE_SLUG}/image`,
  installed,
});

const useInstalledTiles = (): Tile[] => {
  const { data } = useSuspenseQuery({ ...getInstalledAppsOptions() });
  const indexUrl = useIndexUrl();
  const installed = data.installed.filter((a) => a.info.available);
  const ids = installed.filter((a) => a.info.urn.endsWith(`:${STORE_SLUG}`)).map((a) => a.info.id);
  const { data: meta = [] } = useQuery({
    queryKey: ['wizard-index-meta', indexUrl, [...ids].sort().join(',')],
    queryFn: () => lookupIndex(indexUrl, ids),
    enabled: ids.length > 0,
    staleTime: 5 * 60 * 1000,
  });
  const byId = new Map(meta.map((m) => [m.id, m]));
  return installed.map(({ info, app, metadata }) => {
    const [appName = info.id, storeId = ''] = info.urn.split(':');
    const m = storeId === STORE_SLUG ? byId.get(appName) : undefined;
    const cats = (info.categories ?? []) as string[];
    return {
      id: appName,
      name: info.name,
      desc: m?.summary ?? info.short_desc,
      kind: storeId === STORE_SLUG ? kindOf(m?.category ?? cats[0], m?.type) : 'other',
      screen: m ? hasScreen(m.type) : !info.no_gui,
      uses: m?.uses ?? [],
      href: `/apps/${storeId}/${appName}`,
      logo: `/api/marketplace/apps/${info.urn}/image`,
      status: app.status,
      update: Number(app.version) < Number(metadata.latestVersion) && app.ignoredVersion !== metadata.latestVersion,
    };
  });
};

const match = (t: Tile, q: string) => `${t.name} ${t.desc ?? ''}`.toLowerCase().includes(q.toLowerCase());

/* ---- My Apps: the launcher ------------------------------------------------------------------------------------ */

export const MyAppsView = () => {
  const tiles = useInstalledTiles();
  const indexUrl = useIndexUrl();
  const [q, setQ] = useState('');
  const dq = useDebounced(q.trim(), 250);
  const mine = new Set(tiles.map((t) => t.id));
  const store = useQuery({
    queryKey: ['wizard-index', indexUrl, dq],
    queryFn: () => searchIndex(indexUrl, dq, 10),
    enabled: dq.length > 1,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });

  const getMore = (
    <Link to="/app-store" className="lapp" aria-label="Get apps">
      <span className="ic" style={{ background: 'linear-gradient(145deg, #8f63ff, #ff5ccf)' }}>
        +
      </span>
      <span className="nm">Get apps</span>
    </Link>
  );

  const hits = q.trim() ? tiles.filter((t) => match(t, q.trim())) : [];
  const more = (store.data ?? []).filter((a) => !mine.has(a.id)).slice(0, 5);

  return (
    <Sheet search={<SearchBar value={q} onChange={setQ} placeholder="Search apps" />}>
      {q.trim() ? (
        <>
          {hits.length ? (
            <div className="lgroup">
              <h4>Your apps</h4>
              {hits.map((t) => (
                <AppRow key={t.id} t={t} tag="Open" />
              ))}
            </div>
          ) : null}
          {more.length ? (
            <div className="lgroup">
              <h4>App Store</h4>
              {more.map((a) => (
                <AppRow key={a.id} t={tileFromIndex(a, false)} tag="Get" />
              ))}
            </div>
          ) : null}
          {!hits.length && !more.length ? <div className="noresult">{store.isFetching ? 'Searching…' : 'Nothing found'}</div> : null}
        </>
      ) : (
        <TileGroups tiles={tiles} extra={{ apps: getMore }} />
      )}
    </Sheet>
  );
};

/* ---- App Store: the same sheet over the index ---------------------------------------------------------------- */

export const AppStoreView = () => {
  const [params] = useSearchParams();
  const indexUrl = useIndexUrl();
  const { data: mine } = useQuery({ ...getInstalledAppsOptions() });
  const installed = new Set((mine?.installed ?? []).map((a) => a.info.urn));
  const [q, setQ] = useState(params.get('q') ?? '');
  const dq = useDebounced(q.trim(), 250);

  const all = useQuery({ queryKey: ['wizard-index', indexUrl, ''], queryFn: () => searchIndex(indexUrl, ''), staleTime: 60_000 });
  const hits = useQuery({
    queryKey: ['wizard-index', indexUrl, dq],
    queryFn: () => searchIndex(indexUrl, dq, 20),
    enabled: dq.length > 0,
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
  const toTile = (a: IndexApp) => tileFromIndex(a, installed.has(`${a.id}:${STORE_SLUG}`));

  return (
    <Sheet search={<SearchBar value={q} onChange={setQ} placeholder="Search the App Store" />}>
      {all.isLoading ? <div className="noresult">Loading…</div> : null}
      {all.isError ? <div className="noresult">The App Store can’t be reached right now</div> : null}
      {dq ? (
        (hits.data ?? []).length ? (
          <div className="lgroup">
            <h4>Results</h4>
            {(hits.data ?? []).map((a) => (
              <AppRow key={a.id} t={toTile(a)} />
            ))}
          </div>
        ) : (
          <div className="noresult">{hits.isFetching ? 'Searching…' : 'Nothing found'}</div>
        )
      ) : (
        <TileGroups tiles={(all.data ?? []).map(toTile)} />
      )}
    </Sheet>
  );
};

/* ---- Resources: a group for the Settings sheet ---------------------------------------------------------------- */

const ResourceRow = ({ icon, title, note, value, pct }: { icon: ReactNode; title: string; note: string; value: string; pct: number }) => (
  <div className="st-row">
    <span className="st-ic">{icon}</span>
    <span className="st-tx">
      <b>{title}</b>
      <small>{note}</small>
    </span>
    <span className={clsx('st-meter', { hot: pct >= 85 })}>
      <em>{value}</em>
      <span className="bar">
        <i style={{ width: `${Math.max(2, Math.min(100, pct)).toFixed(0)}%` }} />
      </span>
    </span>
  </div>
);

export const ResourcesView = () => {
  const { data } = useSuspenseQuery({ ...systemLoadOptions(), refetchInterval: 3000 });
  const { data: apps } = useQuery({ ...getInstalledAppsOptions() });
  const { updatesAvailable } = useAppContext();
  const count = apps?.installed.length ?? 0;
  const memUsed = (data.memoryTotal * data.percentUsedMemory) / 100;

  return (
    <div className="wux">
      <div className="wux-sheet auto">
        <div className="wux-body top">
          <div className="st-group">
            <h4>This Wizard</h4>
            <ResourceRow
              icon={<IconDatabase />}
              title="Storage"
              note={`${data.diskUsed} of ${data.diskSize} GB used`}
              value={`${data.percentUsed}%`}
              pct={data.percentUsed}
            />
            <ResourceRow
              icon={<IconCpu />}
              title="Processor"
              note="Uninstall apps to reduce load"
              value={`${Math.round(data.cpuLoad)}%`}
              pct={data.cpuLoad}
            />
            <ResourceRow
              icon={<IconCircuitResistor />}
              title="Memory"
              note={`${memUsed.toFixed(1)} of ${data.memoryTotal} GB used`}
              value={`${data.percentUsedMemory}%`}
              pct={data.percentUsedMemory}
            />
          </div>
          {count > 0 ? (
            <div className="st-group">
              <h4>Apps</h4>
              <div className="st-actions">
                <StartAllButton availableUpdates={updatesAvailable} />
                <StopAllButton availableUpdates={updatesAvailable} />
                <RestartAllButton availableUpdates={updatesAvailable} />
              </div>
              <p className="st-note">
                {count} app{count === 1 ? '' : 's'} installed
              </p>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
};
