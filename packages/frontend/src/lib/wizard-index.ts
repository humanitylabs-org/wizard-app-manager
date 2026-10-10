// Read-only access to the public Wizard App Index from the browser (the same index the backend installs from).
// Browsing asks the index; nothing is downloaded to the box until you install.
import type { GraphKind, GraphNode } from '@/components/app-graph/app-graph';

export const STORE_SLUG = 'wizardapps';
export const DEFAULT_INDEX = 'https://apps.aiwizards.com';

export type IndexApp = {
  id: string;
  name: string;
  summary: string;
  type?: string;
  category?: string;
  uses?: string[];
  provides?: string[];
  logo?: string | null;
};

type Search = { apps: IndexApp[]; next_offset: number | null };

const clean = (u?: string) => (u && /^https?:\/\//.test(u) && !/github\.com|\.git$/.test(u) ? u : DEFAULT_INDEX).replace(/\/+$/, '');

export async function searchIndex(indexUrl: string | undefined, q: string, limit = 50): Promise<IndexApp[]> {
  const base = clean(indexUrl);
  const out: IndexApp[] = [];
  let offset: number | null = 0;
  // Up to 4 pages (200 apps) per view; search narrows it down at scale.
  for (let page = 0; page < 4 && offset != null; page++) {
    const r = await fetch(`${base}/v1/search?${new URLSearchParams({ q, limit: String(limit), offset: String(offset) })}`);
    if (!r.ok) throw new Error(`index ${r.status}`);
    const d = (await r.json()) as Search;
    out.push(...d.apps);
    offset = d.next_offset;
  }
  return out;
}

export async function lookupIndex(indexUrl: string | undefined, ids: string[]): Promise<IndexApp[]> {
  const base = clean(indexUrl);
  const res = await Promise.all(
    [...new Set(ids)].map(async (id) => {
      try {
        const r = await fetch(`${base}/v1/search?${new URLSearchParams({ id, limit: '3' })}`);
        if (!r.ok) return null;
        return ((await r.json()) as Search).apps.find((a) => a.id === id) ?? null;
      } catch {
        return null;
      }
    }),
  );
  return res.filter(Boolean) as IndexApp[];
}

const KINDS: GraphKind[] = ['agent', 'interface', 'app', 'system'];
export const kindOf = (category?: string, type?: string): GraphKind =>
  (KINDS as string[]).includes(category ?? '') ? (category as GraphKind) : type === 'agent' ? 'agent' : 'app';

/** Service apps have no screen; agents, views and external apps open in the browser. */
export const hasScreen = (type?: string) => type !== 'service';

export const logoFor = (id: string) => `/api/marketplace/apps/${id}:${STORE_SLUG}/image`;

export const nodeFromIndex = (a: IndexApp, href: string, extra: Partial<GraphNode> = {}): GraphNode => ({
  id: a.id,
  name: a.name,
  desc: a.summary,
  kind: kindOf(a.category, a.type),
  screen: hasScreen(a.type),
  uses: a.uses ?? [],
  href,
  logo: logoFor(a.id),
  ...extra,
});
