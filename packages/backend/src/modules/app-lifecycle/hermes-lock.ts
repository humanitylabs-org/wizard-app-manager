// One authority: the same JSON packaged by wam and the public installer.
import policy from '../../../../../wam/hermes-lock.json';
import { ConflictException } from '@nestjs/common';

export const hermesRecipe = policy.agent_recipe;

export const isHermes = (appUrn: string) => appUrn.split(':', 1)[0] === 'hermes-agent';

type Promotion = {
  id: string;
  published_at: string;
  approved_at: string;
  recommendation: string;
  approval: string;
  acceptance: string;
  canary: string;
  bug_review: string;
  rollback: string;
};

/** Request data is not approval: only a reviewed, bundled policy can approve. */
export function assertHermesUpdate(appUrn: string, requestedId?: string, now = Date.now(), source: unknown = policy) {
  if (!isHermes(appUrn)) return;
  const lock = source as { schema: number; automatic_updates: string; minimum_release_age_days: number; state: string; promotion: Promotion | null };
  const p = lock.promotion;
  const published = p ? Date.parse(p.published_at) : Number.NaN;
  const approved = p ? Date.parse(p.approved_at) : Number.NaN;
  const evidence = p && [p.recommendation, p.approval, p.acceptance, p.canary, p.bug_review, p.rollback];
  if (
    lock.schema !== 1 ||
    lock.automatic_updates !== 'paused' ||
    lock.minimum_release_age_days !== 7 ||
    lock.state !== 'approved' ||
    !p ||
    !/^[a-z0-9][a-z0-9.-]{0,63}$/.test(p.id) ||
    !requestedId ||
    requestedId !== p.id ||
    !/(Z|[+-]\d{2}:\d{2})$/.test(p.published_at) ||
    !/(Z|[+-]\d{2}:\d{2})$/.test(p.approved_at) ||
    !Number.isFinite(published) ||
    !Number.isFinite(approved) ||
    approved < published + 7 * 86400_000 ||
    approved > now ||
    !evidence?.every((url) => typeof url === 'string' && /^https:\/\/\S+$/.test(url))
  ) {
    throw new ConflictException('Hermes updates are paused: a reviewed shared-lock promotion and explicit id are required');
  }
}
