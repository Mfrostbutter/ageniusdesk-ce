/**
 * Workflow Promotion view — move workflows between registered n8n instances.
 *
 * Flow: pick source + target, select workflows, run a preflight (shows the
 * credentials each workflow needs + whether the target supports the type +
 * duplicate-name warnings), map source creds -> target cred ids, then promote.
 *
 * The preflight is the point: nothing imports silently broken. A workflow with
 * an unmapped credential imports but is refused activation until it's linked.
 */

import { get, post } from '../api.js';

let _instances = [];
let _preflight = null;   // last preflight result

function esc(s) { const el = document.createElement('span'); el.textContent = s == null ? '' : s; return el.innerHTML; }

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <div>
        <h2 class="section-title">Workflow Promotion</h2>
        <span style="font-size:12px;color:var(--text-secondary)">Move workflows across instances (dev &rarr; staging &rarr; prod). Credentials are mapped, never silently dropped.</span>
      </div>
    </div>
    <div class="card" style="padding:16px">
      <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end">
        <label style="flex:1;min-width:200px;margin:0">
          <span style="font-size:12px;color:var(--text-secondary)">Source instance</span>
          <select id="promo-source"></select>
        </label>
        <div style="font-size:20px;color:var(--text-secondary);padding-bottom:6px">&rarr;</div>
        <label style="flex:1;min-width:200px;margin:0">
          <span style="font-size:12px;color:var(--text-secondary)">Target instance</span>
          <select id="promo-target"></select>
        </label>
        <button class="btn btn-sm btn-ghost" id="promo-reload-wf">Load workflows</button>
      </div>
      <div id="promo-wf-wrap" style="margin-top:16px">
        <div class="empty-state" style="padding:20px"><p>Pick a source instance and load its workflows.</p></div>
      </div>
    </div>
    <div id="promo-preflight" style="margin-top:16px"></div>
    <div id="promo-results" style="margin-top:16px"></div>
  `;

  try {
    const d = await get('/api/n8n/instances');
    _instances = d.instances || [];
  } catch (e) {
    container.querySelector('#promo-wf-wrap').innerHTML =
      `<div class="empty-state"><p>Failed to load instances: ${esc(e.message)}</p></div>`;
    return;
  }

  const src = document.getElementById('promo-source');
  const tgt = document.getElementById('promo-target');
  const opts = _instances.map(i => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join('');
  src.innerHTML = opts;
  tgt.innerHTML = opts;
  if (_instances.length > 1) tgt.selectedIndex = 1;   // default target != source

  document.getElementById('promo-reload-wf').addEventListener('click', loadWorkflows);
  src.addEventListener('change', loadWorkflows);
  if (_instances.length) loadWorkflows();
}

async function loadWorkflows() {
  const wrap = document.getElementById('promo-wf-wrap');
  const sourceId = document.getElementById('promo-source').value;
  document.getElementById('promo-preflight').innerHTML = '';
  document.getElementById('promo-results').innerHTML = '';
  if (!sourceId) { wrap.innerHTML = '<div class="empty-state"><p>Select a source instance.</p></div>'; return; }
  wrap.innerHTML = '<div class="spinner"></div>';
  try {
    const d = await get(`/api/promote/workflows/${encodeURIComponent(sourceId)}`);
    const wfs = d.workflows || [];
    if (!wfs.length) { wrap.innerHTML = '<div class="empty-state"><p>No workflows on this instance.</p></div>'; return; }
    wrap.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <strong style="font-size:13px">${wfs.length} workflow${wfs.length === 1 ? '' : 's'}</strong>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm btn-ghost" id="promo-all">Select all</button>
          <button class="btn btn-sm btn-ghost" id="promo-none">Clear</button>
          <button class="btn btn-sm btn-primary" id="promo-preflight-btn">Preflight &rarr;</button>
        </div>
      </div>
      <div style="max-height:340px;overflow:auto;border:1px solid var(--border-dim);border-radius:var(--radius)">
        ${wfs.map(w => `
          <label style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--border-dim);cursor:pointer;margin:0">
            <input type="checkbox" class="promo-wf" value="${esc(w.id)}" style="width:auto;margin:0">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(w.name)}</span>
            <span style="font-size:10px;padding:2px 6px;border-radius:var(--radius);background:var(--bg-input);color:var(--text-secondary)">${w.active ? 'active' : 'inactive'}</span>
            <span style="font-size:10px;color:var(--text-secondary);font-family:var(--font-mono)">${esc(w.trigger_type || '')}</span>
          </label>`).join('')}
      </div>`;
    document.getElementById('promo-all').addEventListener('click', () => setAll(true));
    document.getElementById('promo-none').addEventListener('click', () => setAll(false));
    document.getElementById('promo-preflight-btn').addEventListener('click', runPreflight);
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state"><p>Failed to load workflows: ${esc(e.message)}</p></div>`;
  }
}

function setAll(v) { document.querySelectorAll('.promo-wf').forEach(c => { c.checked = v; }); }

function selectedWorkflowIds() {
  return Array.from(document.querySelectorAll('.promo-wf:checked')).map(c => c.value);
}

async function runPreflight() {
  const ids = selectedWorkflowIds();
  const pf = document.getElementById('promo-preflight');
  document.getElementById('promo-results').innerHTML = '';
  if (!ids.length) { pf.innerHTML = '<div class="card" style="padding:12px;color:var(--text-secondary)">Select at least one workflow.</div>'; return; }
  pf.innerHTML = '<div class="spinner"></div>';
  try {
    _preflight = await post('/api/promote/preflight', {
      source_instance_id: document.getElementById('promo-source').value,
      target_instance_id: document.getElementById('promo-target').value,
      workflow_ids: ids,
    });
    renderPreflight(_preflight);
  } catch (e) {
    pf.innerHTML = `<div class="card" style="padding:12px;color:#ff6d5a">Preflight failed: ${esc(e.message)}</div>`;
  }
}

function renderPreflight(p) {
  const pf = document.getElementById('promo-preflight');
  const wfRows = (p.workflows || []).map(w => {
    if (!w.ok) return `<tr><td>${esc(w.workflow_id)}</td><td colspan="3" style="color:#ff6d5a">${esc(w.error)}</td></tr>`;
    const dup = w.duplicate_on_target
      ? `<span style="color:#ff9f43" title="A workflow with this name already exists on the target — promoting creates a duplicate.">&#9888; duplicate name</span>`
      : '<span style="color:var(--text-secondary)">new</span>';
    const creds = (w.credentials || []).length;
    return `<tr>
      <td>${esc(w.name)}</td>
      <td style="text-align:center">${w.node_count}</td>
      <td style="text-align:center">${creds}</td>
      <td>${dup}</td>
    </tr>`;
  }).join('');

  const creds = p.credentials_to_map || [];
  const introspect = p.target_supports_schema_introspection;
  const credRows = creds.map(c => {
    let support = '';
    if (c.type_supported_on_target === false) {
      support = `<span style="color:#ff6d5a" title="The target instance does not ship this credential type.">&#9888; type not on target</span>`;
    } else if (c.type_supported_on_target === true) {
      support = `<span style="color:#10b981">supported</span>`;
    } else {
      support = `<span style="color:var(--text-secondary)">unknown</span>`;
    }
    const sid = c.source_id || '';
    return `<tr>
      <td><code style="font-size:11px">${esc(c.cred_type)}</code></td>
      <td>${esc(c.name)}</td>
      <td><code style="font-size:11px">${esc(sid)}</code></td>
      <td>${support}</td>
      <td><input class="promo-credmap" data-sid="${esc(sid)}" placeholder="target cred id" style="margin:0;font-size:12px;padding:4px 8px" ${sid ? '' : 'disabled'}></td>
      <td><input class="promo-credname" data-sid="${esc(sid)}" placeholder="target name (optional)" style="margin:0;font-size:12px;padding:4px 8px" ${sid ? '' : 'disabled'}></td>
      <td class="promo-credstatus" data-sid="${esc(sid)}" style="font-size:11px;color:var(--text-secondary);white-space:nowrap">&mdash;</td>
    </tr>`;
  }).join('');

  pf.innerHTML = `
    <div class="card" style="padding:16px">
      <h3 style="margin:0 0 4px 0;font-size:15px">Preflight: ${esc(p.source.name)} &rarr; ${esc(p.target.name)}</h3>
      <p style="font-size:12px;color:var(--text-secondary);margin:0 0 12px 0">Review what promoting will do. Map each credential to a target credential id before running.</p>
      <table class="data-table" style="width:100%;font-size:13px;margin-bottom:16px">
        <thead><tr><th>Workflow</th><th style="text-align:center">Nodes</th><th style="text-align:center">Creds</th><th>On target</th></tr></thead>
        <tbody>${wfRows}</tbody>
      </table>
      <div style="display:flex;align-items:center;gap:12px;margin:0 0 6px 0;flex-wrap:wrap">
        <h4 style="margin:0;font-size:13px">Credential mapping ${creds.length ? `(${creds.length})` : ''}</h4>
        ${creds.length ? '<button class="btn btn-sm btn-primary" id="promo-autoprov" title="Create or reuse the target credentials from AgeniusDesk\'s Secrets store, and fill the mapping automatically">&#9889; Auto-provision from Secrets</button>' : ''}
        <span id="promo-autoprov-msg" style="font-size:12px;color:var(--text-secondary)"></span>
      </div>
      ${creds.length ? `
        ${introspect ? '' : '<p style="font-size:11px;color:#ff9f43;margin:0 0 8px 0">&#9888; Could not introspect the target\'s credential types (no schema access) — "supported" column is unknown.</p>'}
        <p style="font-size:11px;color:var(--text-secondary);margin:0 0 8px 0"><b>Auto-provision</b> reuses a credential AgeniusDesk already created on the target, or creates one from a matching secret in your Secrets store. It cannot see credentials you made by hand in n8n (n8n has no list-credentials API) — for those, paste the target cred id <b>and its exact name</b> (n8n binds by id AND name; a mismatched name imports the workflow unbound). Leave a row blank to import unlinked (the workflow will not be activated).</p>
        <table class="data-table" style="width:100%;font-size:12px">
          <thead><tr><th>Type</th><th>Source name</th><th>Source id</th><th>Target type</th><th>Target cred id</th><th>Target name</th><th>Status</th></tr></thead>
          <tbody>${credRows}</tbody>
        </table>`
        : '<p style="font-size:12px;color:var(--text-secondary)">These workflows reference no credentials.</p>'}
      <div style="display:flex;gap:16px;align-items:center;margin-top:16px;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:6px;margin:0;font-size:13px"><input type="checkbox" id="promo-activate" style="width:auto;margin:0">Activate on target</label>
        <label style="display:flex;align-items:center;gap:6px;margin:0;font-size:13px"><input type="checkbox" id="promo-dryrun" style="width:auto;margin:0">Dry run (no writes)</label>
        <label style="display:flex;align-items:center;gap:6px;margin:0;font-size:13px">Name suffix <input id="promo-suffix" placeholder="e.g. (prod)" style="margin:0;width:120px;font-size:12px;padding:4px 8px"></label>
        <button class="btn btn-primary" id="promo-run-btn" style="margin-left:auto">Promote</button>
      </div>
    </div>`;
  document.getElementById('promo-run-btn').addEventListener('click', runPromote);
  const ap = document.getElementById('promo-autoprov');
  if (ap) ap.addEventListener('click', autoProvision);
}

function credStatusLabel(r) {
  if (r.method === 'provisioned') return '✓ created';
  if (r.method === 'reused') return '✓ reused';
  if (r.method === 'error') return '✗ ' + (r.note || 'error');
  if (r.method === 'ambiguous' || r.method === 'no_secret') return '⚠ ' + (r.note || '');
  return r.note || '—';
}
function credStatusColor(r) {
  if (r.method === 'provisioned' || r.method === 'reused') return '#10b981';
  if (r.method === 'error') return '#ff6d5a';
  if (r.method === 'ambiguous' || r.method === 'no_secret') return '#ff9f43';
  return 'var(--text-secondary)';
}

async function autoProvision() {
  const btn = document.getElementById('promo-autoprov');
  const msg = document.getElementById('promo-autoprov-msg');
  const allCreds = (_preflight.credentials_to_map || []).filter(c => c.source_id);
  if (!allCreds.length || !_preflight.target) return;

  const idInputFor = sid => document.querySelector(`input.promo-credmap[data-sid="${sid}"]`);
  const statusFor  = sid => document.querySelector(`td.promo-credstatus[data-sid="${sid}"]`);

  // Respect rows the user already filled in — mark them and DON'T re-provision
  // (avoids overwriting a manual id or creating a duplicate credential).
  let manualCount = 0;
  const toDo = allCreds.filter(c => {
    const inp = idInputFor(c.source_id);
    const hasManual = inp && inp.value.trim();
    if (hasManual) {
      manualCount++;
      const st = statusFor(c.source_id);
      if (st) { st.textContent = '✓ set manually'; st.style.color = '#10b981'; }
      return false;
    }
    return true;
  });

  if (!toDo.length) {
    msg.textContent = `All ${allCreds.length} credential(s) already mapped manually — nothing to auto-provision.`;
    return;
  }

  btn.disabled = true; const orig = btn.innerHTML; btn.textContent = 'Provisioning…';
  msg.textContent = '';
  try {
    const out = await post('/api/promote/auto-provision', {
      target_instance_id: _preflight.target.id,
      credentials: toDo.map(c => ({ cred_type: c.cred_type, source_id: c.source_id, name: c.name })),
    });
    (out.resolutions || []).forEach(r => {
      const idInput = idInputFor(r.source_id);
      const nameInput = document.querySelector(`input.promo-credname[data-sid="${r.source_id}"]`);
      const st = statusFor(r.source_id);
      if (r.target_id && idInput) idInput.value = r.target_id;
      if (r.target_name && nameInput) nameInput.value = r.target_name;
      if (st) { st.textContent = credStatusLabel(r); st.style.color = credStatusColor(r); }
    });
    const parts = [`${out.provisioned} created`, `${out.reused} reused`];
    if (manualCount) parts.push(`${manualCount} manual`);
    parts.push(`${out.unresolved} need attention`);
    msg.textContent = parts.join(' · ');
  } catch (e) {
    msg.textContent = 'Auto-provision failed: ' + e.message;
  } finally {
    btn.disabled = false; btn.innerHTML = orig;
  }
}

function collectCredMaps() {
  const cred_map = {};
  const cred_names = {};
  document.querySelectorAll('.promo-credmap').forEach(inp => {
    const sid = inp.dataset.sid; const v = (inp.value || '').trim();
    if (sid && v) cred_map[sid] = v;
  });
  document.querySelectorAll('.promo-credname').forEach(inp => {
    const sid = inp.dataset.sid; const v = (inp.value || '').trim();
    if (sid && v) cred_names[sid] = v;
  });
  return { cred_map, cred_names };
}

async function runPromote() {
  const res = document.getElementById('promo-results');
  const btn = document.getElementById('promo-run-btn');
  const ids = (_preflight.workflows || []).filter(w => w.ok).map(w => w.workflow_id);
  const { cred_map, cred_names } = collectCredMaps();
  const activate = document.getElementById('promo-activate').checked;
  const dry_run = document.getElementById('promo-dryrun').checked;
  const name_suffix = (document.getElementById('promo-suffix').value || '').trim();

  // n8n binds a node credential by id AND name: an id with the wrong/blank name
  // imports but shows unbound in the editor. Require a name for every set id.
  const missingName = Object.keys(cred_map).filter(sid => !cred_names[sid]);
  if (missingName.length) {
    missingName.forEach(sid => {
      const st = document.querySelector(`td.promo-credstatus[data-sid="${sid}"]`);
      if (st) { st.textContent = '⚠ needs Target name'; st.style.color = '#ff9f43'; }
    });
    res.innerHTML = `<div class="card" style="padding:12px;color:#ff9f43">${missingName.length} credential(s) have an id but no <b>Target name</b>. n8n binds by id <i>and</i> name, so a blank name imports the workflow unbound. Enter the exact target credential name (or use Auto-provision, which fills it).</div>`;
    return;
  }

  const unmappedCount = (_preflight.credentials_to_map || []).filter(c => c.source_id && !cred_map[c.source_id]).length;
  if (!dry_run && unmappedCount > 0) {
    if (!confirm(`${unmappedCount} credential(s) are unmapped. Those workflows will import but cannot be activated until you link the credential on the target. Continue?`)) return;
  }

  btn.disabled = true; btn.textContent = dry_run ? 'Simulating...' : 'Promoting...';
  res.innerHTML = '<div class="spinner"></div>';
  try {
    const out = await post('/api/promote/run', {
      source_instance_id: document.getElementById('promo-source').value,
      target_instance_id: document.getElementById('promo-target').value,
      workflow_ids: ids, cred_map, cred_names, activate, name_suffix, dry_run,
    });
    renderResults(out);
  } catch (e) {
    res.innerHTML = `<div class="card" style="padding:12px;color:#ff6d5a">Promote failed: ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Promote';
  }
}

function renderResults(out) {
  const res = document.getElementById('promo-results');
  const rows = (out.results || []).map(r => {
    const ok = r.success;
    const status = ok
      ? (out.dry_run ? '<span style="color:#48dbfb">dry run</span>' : '<span style="color:#10b981">promoted</span>')
      : `<span style="color:#ff6d5a">failed</span>`;
    const detail = ok
      ? [
          r.target_workflow_id ? `id ${esc(r.target_workflow_id)}` : '',
          (r.unmapped_credentials && r.unmapped_credentials.length) ? `<span style="color:#ff9f43">${r.unmapped_credentials.length} unmapped cred(s)</span>` : '',
          r.activated ? '<span style="color:#10b981">activated</span>' : (r.activation_error ? `<span style="color:#ff9f43">${esc(r.activation_error)}</span>` : ''),
          r.warning ? `<span style="color:#ff9f43">${esc(r.warning)}</span>` : '',
        ].filter(Boolean).join(' &middot; ')
      : esc(r.error || 'unknown error');
    return `<tr><td>${esc(r.name || r.workflow_id)}</td><td>${status}</td><td style="font-size:12px">${detail}</td></tr>`;
  }).join('');
  res.innerHTML = `
    <div class="card" style="padding:16px">
      <h3 style="margin:0 0 8px 0;font-size:15px">${out.dry_run ? 'Dry run' : 'Promotion'}: ${out.promoted} ok, ${out.failed} failed</h3>
      <table class="data-table" style="width:100%;font-size:13px">
        <thead><tr><th>Workflow</th><th>Status</th><th>Detail</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}
