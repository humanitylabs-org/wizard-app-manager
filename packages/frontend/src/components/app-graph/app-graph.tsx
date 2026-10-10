import { IconCloudOff } from '@tabler/icons-react';
import clsx from 'clsx';
import { type ReactNode, useId, useLayoutEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import './app-graph.css';

/*
 * Wizard app graph (n8n-style). Each agent or app is a flow:
 *   interfaces that open it  ->  the agent / app  ->  the system apps it uses
 * Interfaces are not a shelf of their own; they sit next to the thing they are a screen for.
 * Every node says whether it has a screen or is headless (used through your agent).
 * Shared by My Apps (installed, live status) and the App Store (everything in the index).
 */

export type GraphKind = 'agent' | 'interface' | 'app' | 'system' | 'other';

export type GraphNode = {
  id: string;
  name: string;
  desc?: string;
  kind: GraphKind;
  screen: boolean;
  uses: string[];
  href: string;
  logo?: string;
  status?: string;
  update?: boolean;
  installed?: boolean;
  ghost?: boolean;
};

type Role = 'in' | 'main' | 'out' | 'solo';

const KIND: Record<GraphKind, string> = { agent: 'Agent', interface: 'Interface', app: 'App', system: 'System app', other: 'App' };

const pretty = (id: string) => id.replace(/^wizard-/, '').replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

let GHOST_LABEL = 'Not installed';

const NodeCard = ({ n, pos, extra }: { n: GraphNode; pos: Role; extra?: ReactNode }) => (
  <Link
    to={n.href}
    className={clsx('gnode', `k-${n.kind}`, `r-${pos}`, { ghost: n.ghost })}
    data-role={pos}
    data-live={n.status === 'running' ? '1' : '0'}
    data-ghost={n.ghost ? '1' : '0'}
    title={n.desc}
    aria-label={n.name}
  >
    <span className="gport l" aria-hidden="true" />
    <span className="gport r" aria-hidden="true" />
    <span className="gic">
      {n.logo ? (
        <img
          src={n.logo}
          alt=""
          loading="lazy"
          onError={(e) => {
            e.currentTarget.style.visibility = 'hidden';
          }}
        />
      ) : null}
      {n.status ? <i className={`gdot ${n.status}`} /> : null}
      {n.update ? <b className="gup">↑</b> : null}
    </span>
    <span className="gtx">
      <span className="gnm">{n.name}</span>
      <span className="gmeta">
        <span className="gkind">{n.ghost ? GHOST_LABEL : KIND[n.kind]}</span>
        <span className={clsx('gscreen', n.screen ? 'on' : 'off')}>{n.screen ? 'Screen' : 'Headless'}</span>
        {n.installed && !n.status ? <span className="gok">✓ Installed</span> : null}
      </span>
      {extra ? <span className="gextra">{extra}</span> : null}
    </span>
  </Link>
);

type Wire = { d: string; ghost: boolean; live: boolean };

const Flow = ({ main, ins, outs, animate }: { main: GraphNode; ins: GraphNode[]; outs: GraphNode[]; animate?: boolean }) => {
  const ref = useRef<HTMLDivElement>(null);
  const [wires, setWires] = useState<Wire[]>([]);
  const gid = useId().replace(/:/g, '');

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const draw = () => {
      const box = el.getBoundingClientRect();
      const m = el.querySelector<HTMLElement>('[data-role="main"]');
      if (!m) return;
      const rel = (r: DOMRect) => ({
        l: r.left - box.left,
        r: r.right - box.left,
        t: r.top - box.top,
        b: r.bottom - box.top,
        cx: (r.left + r.right) / 2 - box.left,
        cy: (r.top + r.bottom) / 2 - box.top,
      });
      const M = rel(m.getBoundingClientRect());
      const mainLive = m.dataset.live === '1';
      const next: Wire[] = [];
      for (const n of el.querySelectorAll<HTMLElement>('[data-role="in"],[data-role="out"]')) {
        const R = rel(n.getBoundingClientRect());
        const isIn = n.dataset.role === 'in';
        const ghost = n.dataset.ghost === '1';
        const beside = isIn ? R.r <= M.l + 2 : R.l >= M.r - 2;
        let d: string;
        if (beside) {
          const [x1, y1, x2, y2] = isIn ? [R.r + 4, R.cy, M.l - 4, M.cy] : [M.r + 4, M.cy, R.l - 4, R.cy];
          const dx = Math.max(24, (x2 - x1) / 2);
          d = `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
        } else {
          const [x1, y1, x2, y2] = isIn ? [R.cx, R.b + 2, M.cx, M.t - 2] : [M.cx, M.b + 2, R.cx, R.t - 2];
          const dy = Math.max(12, (y2 - y1) / 2);
          d = `M${x1} ${y1} C${x1} ${y1 + dy} ${x2} ${y2 - dy} ${x2} ${y2}`;
        }
        next.push({ d, ghost, live: Boolean(animate) && !ghost && mainLive && n.dataset.live === '1' });
      }
      setWires(next);
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(el);
    for (const img of el.querySelectorAll('img')) img.addEventListener('load', draw);
    return () => ro.disconnect();
  }, [animate]);

  return (
    <div className={clsx('gflow', `k-${main.kind}`)} ref={ref}>
      <svg className="gwires" aria-hidden="true">
        <defs>
          <linearGradient id={`g${gid}`} x1="0" x2="1" y1="0" y2="0">
            <stop offset="0" stopColor="#3fe0ff" stopOpacity=".75" />
            <stop offset="1" stopColor="#b99bff" stopOpacity=".85" />
          </linearGradient>
        </defs>
        {wires.map((w) => (
          <g key={w.d}>
            {!w.ghost ? <path className="gwire-glow" d={w.d} /> : null}
            <path className={clsx('gwire', { ghost: w.ghost, live: w.live })} d={w.d} stroke={w.ghost ? undefined : `url(#g${gid})`} />
          </g>
        ))}
      </svg>
      <div className="gcol in">
        <span className="gcolh">Interface</span>
        {ins.length ? (
          ins.map((n) => <NodeCard key={n.id} n={n} pos="in" />)
        ) : (
          <div className="gnode none r-in" data-role="in" data-ghost="1">
            <span className="gic">
              <IconCloudOff size={20} />
            </span>
            <span className="gtx">
              <span className="gnm">No interface</span>
              <span className="gmeta">{main.screen ? 'It has its own screen' : 'Use it by asking your agent'}</span>
            </span>
          </div>
        )}
      </div>
      <div className="gcol main">
        <span className="gcolh">{KIND[main.kind]}</span>
        <NodeCard n={main} pos="main" extra={main.desc} />
      </div>
      <div className="gcol out">
        <span className="gcolh">Uses</span>
        {outs.length ? outs.map((n) => <NodeCard key={n.id} n={n} pos="out" />) : <span className="gnone">Needs no other apps</span>}
      </div>
    </div>
  );
};

export const AppGraph = ({
  nodes,
  pool,
  animate,
  missingHref,
  ghostLabel = 'Not installed',
  footer,
}: {
  nodes: GraphNode[];
  /** Extra nodes used only to draw what a node connects to (e.g. apps outside the current search). */
  pool?: GraphNode[];
  animate?: boolean;
  missingHref: (id: string) => string;
  ghostLabel?: string;
  footer?: ReactNode;
}) => {
  GHOST_LABEL = ghostLabel;
  const byId = new Map([...(pool ?? []), ...nodes].map((n) => [n.id, n]));
  const ghost = (id: string, kind: GraphKind): GraphNode => ({ id, name: pretty(id), kind, screen: false, uses: [], href: missingHref(id), ghost: true });

  const primaries = nodes.filter((n) => n.kind === 'agent' || n.kind === 'app');
  const interfaces = nodes.filter((n) => n.kind === 'interface');
  const paired = new Set<string>();

  const flow = (p: GraphNode) => {
    const ins = interfaces.filter((i) => i.uses.includes(p.id));
    for (const i of ins) paired.add(i.id);
    const outs = p.uses.filter((u) => u !== p.id).map((u) => byId.get(u) ?? ghost(u, 'system'));
    return { p, ins, outs };
  };
  const agentFlows = primaries.filter((p) => p.kind === 'agent').map(flow);
  const appFlows = primaries.filter((p) => p.kind === 'app').map(flow);

  // An interface whose app is not here yet still gets its flow, with the app shown as "Not installed".
  const orphanFlows = interfaces
    .filter((i) => !paired.has(i.id))
    .map((i) => {
      const target = i.uses[0];
      return target ? { p: byId.get(target) ?? ghost(target, 'agent'), ins: [i], outs: [] as GraphNode[] } : null;
    });
  const looseInterfaces = interfaces.filter((i) => !paired.has(i.id) && !i.uses[0]);

  const usedBy = (id: string) => [...primaries, ...interfaces].filter((n) => n.uses.includes(id)).map((n) => n.name);
  const system = nodes.filter((n) => n.kind === 'system');
  const other = nodes.filter((n) => n.kind === 'other');

  const section = (title: string, count: number, body: ReactNode) =>
    count ? (
      <section className="gsec">
        <h4>
          {title} <span>{count}</span>
        </h4>
        {body}
      </section>
    ) : null;

  const flows = [...agentFlows, ...orphanFlows.filter((f) => f?.p.kind === 'agent')].filter(Boolean) as ReturnType<typeof flow>[];

  return (
    <div className="wgraph">
      <div className="glegend">
        <span>
          <i className="gscreen on">Screen</i> opens in your browser
        </span>
        <span>
          <i className="gscreen off">Headless</i> works through your agent
        </span>
        <span>
          <svg width="34" height="10" aria-hidden="true">
            <path d="M1 5 C12 5 22 5 33 5" className="gwire" stroke="#b99bff" />
          </svg>
          connected
        </span>
      </div>
      {section(
        'Agents',
        flows.length,
        flows.map((f) => <Flow key={f.p.id} main={f.p} ins={f.ins} outs={f.outs} animate={animate} />),
      )}
      {section(
        'Apps',
        appFlows.length,
        appFlows.map((f) => <Flow key={f.p.id} main={f.p} ins={f.ins} outs={f.outs} animate={animate} />),
      )}
      {section(
        'Interfaces',
        looseInterfaces.length,
        <div className="ggrid">
          {looseInterfaces.map((n) => (
            <NodeCard key={n.id} n={n} pos="solo" />
          ))}
        </div>,
      )}
      {section(
        'System apps',
        system.length,
        <div className="ggrid">
          {system.map((n) => {
            const by = usedBy(n.id);
            return <NodeCard key={n.id} n={n} pos="solo" extra={by.length ? `Used by ${by.join(', ')}` : 'Shared with every agent'} />;
          })}
        </div>,
      )}
      {section(
        'Other',
        other.length,
        <div className="ggrid">
          {other.map((n) => (
            <NodeCard key={n.id} n={n} pos="solo" />
          ))}
        </div>,
      )}
      {footer}
    </div>
  );
};
