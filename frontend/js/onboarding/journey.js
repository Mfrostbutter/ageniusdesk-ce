/**
 * Setup Journey — derives onboarding milestone completion from live app state,
 * never from a stored "step number," so it is always honest and resumable.
 *
 * status() fetches the relevant endpoints in parallel and returns a normalized
 * milestone list. Any probe that errors is treated as not-done; nothing throws.
 */

import { get } from '../api.js';

const FIRST_VISIT_KEY = (view) => `agd_seen:${view}`;

function firstVisited(view) {
  try { return localStorage.getItem(FIRST_VISIT_KEY(view)) === '1'; } catch { return false; }
}

async function safe(promise, fallback) {
  try { return await promise; } catch { return fallback; }
}

/**
 * Returns { milestones: [{id,title,desc,done,optional,cta}], coreDone, coreTotal }.
 * `cta` is { label, run } where run() performs the navigation/action.
 */
export async function status() {
  const [appStatus, instances, secrets, aiCfg, me] = await Promise.all([
    safe(get('/api/status'), {}),
    safe(get('/api/n8n/instances'), { instances: [] }),
    safe(get('/api/admin/secrets'), { secrets: [] }),
    safe(get('/api/assistant/config'), { jobs: {} }),
    safe(get('/api/auth/me'), null),
  ]);

  const hasInstances = (instances.instances || []).length > 0;
  const configured = appStatus.configured === true || hasInstances;
  const hasSecrets = (secrets.secrets || []).length > 0;
  const jobs = (aiCfg && aiCfg.jobs) || {};
  const aiReady = Object.values(jobs).some(j => j && j.provider && j.model);
  const totpOn = !!(me && me.user && me.user.totp && me.user.totp.enabled);
  // `me` is null when login is disabled / edge-managed — hide the 2FA milestone
  // entirely in that case rather than nagging about a factor that can't be set.
  const localAccount = !!(me && me.user);

  const nav = (view, opts) => () => { if (window.__nav) window.__nav(view, opts); };
  const goSettings = (tab) => () => { if (window.__goSettings) window.__goSettings(tab); };

  const milestones = [
    {
      id: 'connect_n8n',
      title: 'Connect or stand up n8n',
      desc: 'Add an existing n8n by URL + API key, or deploy one in a click.',
      done: configured,
      optional: false,
      cta: { label: 'Connect n8n', run: () => { if (window.__openWizard) window.__openWizard(); } },
    },
    localAccount ? {
      id: 'secure',
      title: 'Turn on two-factor',
      desc: 'Protect the dashboard with an authenticator app (recommended).',
      done: totpOn,
      optional: true,
      cta: { label: 'Settings → Account', run: goSettings('account') },
    } : null,
    {
      id: 'secrets',
      title: 'Add your provider keys',
      desc: 'Store API keys once and reference them as $NAME everywhere.',
      done: hasSecrets,
      optional: true,
      cta: { label: 'Open Secrets', run: goSettings('secrets') },
    },
    {
      id: 'ai',
      title: 'Configure the AI assistant',
      desc: 'Pick a provider and model for each assistant area.',
      done: aiReady,
      optional: true,
      cta: { label: 'Settings → AI', run: goSettings('assistant') },
    },
    {
      id: 'explore',
      title: 'Meet the harness',
      desc: 'See the workspace files, sources, and agent instructions.',
      done: firstVisited('knowledge'),
      optional: true,
      cta: { label: 'Open Knowledge', run: nav('knowledge') },
    },
  ].filter(Boolean);

  const core = milestones.filter(m => !m.optional);
  return {
    milestones,
    coreDone: core.filter(m => m.done).length,
    coreTotal: core.length,
  };
}
