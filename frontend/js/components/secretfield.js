/**
 * SecretField — reusable component for credential inputs.
 *
 * Renders a masked input with a "Use existing secret" dropdown and a status
 * line that shows the secret name (either the one this value is currently
 * stored under, or the preview name that will be assigned on save).
 *
 * Usage:
 *   import { secretField } from '../components/secretfield.js';
 *   const field = secretField({
 *     container,
 *     label: 'API Key',
 *     prefix: 'N8N_KEY',
 *     context: 'prod',
 *     initialValue: '',
 *     placeholder: 'Paste your API key',
 *   });
 *   // later:
 *   const val = field.getValue(); // either raw string or "$NAME"
 *
 * The caller decides when to hit /api/admin/secrets/promote — typically at
 * form save time when `val` does not start with "$".
 */

import { get } from '../api.js';

let refsCache = null;
let refsCachePromise = null;

async function loadRefs(force = false) {
  if (force) { refsCache = null; refsCachePromise = null; }
  if (refsCache) return refsCache;
  if (refsCachePromise) return refsCachePromise;
  refsCachePromise = (async () => {
    try {
      const data = await get('/api/admin/secrets/refs');
      refsCache = data.refs || [];
    } catch {
      refsCache = [];
    }
    return refsCache;
  })();
  return refsCachePromise;
}

export function invalidateRefsCache() {
  refsCache = null;
  refsCachePromise = null;
}

function slugify(s) {
  if (!s) return '';
  let out = String(s).replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '').toUpperCase();
  if (out && /^[0-9]/.test(out)) out = '_' + out;
  return out;
}

function computePreviewName(prefix, context) {
  const p = slugify(prefix) || 'SECRET';
  const c = slugify(context);
  return c ? `${p}_${c}` : p;
}

function escHtml(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

export function secretField(opts) {
  const {
    container,
    label = 'API Key',
    prefix = 'SECRET',
    context = '',
    initialValue = '',
    placeholder = 'Paste your credential',
    hint = '',
  } = opts;

  if (!container) throw new Error('secretField: container is required');

  const state = {
    value: initialValue || '',
    prefix,
    context,
    // The ref this input is currently holding (starts with $), if any.
    ref: (initialValue || '').startsWith('$') ? initialValue : null,
    dropdownOpen: false,
  };

  container.innerHTML = `
    <div class="secret-field" style="display:flex;flex-direction:column;gap:4px">
      <label class="secret-field-label" style="font-size:13px;font-weight:500;color:var(--text-primary);display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span class="secret-field-label-text">${escHtml(label)}</span>
        <button type="button" class="secret-field-dropdown-btn btn btn-sm btn-ghost" style="font-size:11px;padding:3px 8px">
          Use existing secret <span style="font-size:9px">&#9662;</span>
        </button>
      </label>
      <div class="secret-field-input-row" style="position:relative">
        <input type="password" class="secret-field-input" placeholder="${escHtml(placeholder)}"
               style="width:100%;box-sizing:border-box;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:8px 10px;color:var(--text-primary);font-size:13px;font-family:var(--font-mono)">
        <div class="secret-field-pill" hidden
             style="display:none;gap:6px;align-items:center;padding:6px 10px;background:var(--bg-input);border:1px solid var(--border-mid);border-radius:var(--radius);font-family:var(--font-mono);font-size:13px;color:var(--info)">
          <span class="secret-field-pill-name"></span>
          <button type="button" class="secret-field-pill-clear btn btn-sm btn-ghost" style="font-size:11px;padding:1px 6px;line-height:1" title="Clear reference">&times;</button>
        </div>
        <div class="secret-field-dropdown" hidden
             style="position:absolute;top:calc(100% + 4px);right:0;min-width:260px;max-width:100%;background:var(--bg-panel-solid);border:1px solid var(--border-mid);border-radius:var(--radius);box-shadow:0 4px 12px rgba(0,0,0,0.35);z-index:1000;max-height:260px;overflow:auto"></div>
      </div>
      <div class="secret-field-status" style="font-size:11px;color:var(--text-dim);min-height:14px;display:flex;align-items:center;gap:8px"></div>
      ${hint ? `<small style="font-size:11px;color:var(--text-dim);margin-top:2px">${escHtml(hint)}</small>` : ''}
    </div>
  `;

  const root = container.querySelector('.secret-field');
  const input = root.querySelector('.secret-field-input');
  const pill = root.querySelector('.secret-field-pill');
  const pillName = root.querySelector('.secret-field-pill-name');
  const pillClear = root.querySelector('.secret-field-pill-clear');
  const dropdownBtn = root.querySelector('.secret-field-dropdown-btn');
  const dropdown = root.querySelector('.secret-field-dropdown');
  const status = root.querySelector('.secret-field-status');

  function updateStatus() {
    if (state.ref) {
      // Stored-as view
      status.innerHTML = `<span style="color:var(--success)">\u2713</span> Stored as <code style="color:var(--info)">${escHtml(state.ref)}</code>`;
    } else if (state.value) {
      const preview = computePreviewName(state.prefix, state.context);
      status.innerHTML = `Will be stored as <code style="color:var(--text-secondary)">$${escHtml(preview)}</code> on save`;
    } else {
      status.textContent = '';
    }
  }

  function renderDropdown(refs) {
    if (!refs.length) {
      dropdown.innerHTML = `<div style="padding:10px;font-size:12px;color:var(--text-dim)">No secrets stored yet. Paste a value above and it will be promoted on save.</div>`;
      return;
    }
    dropdown.innerHTML = refs.map(r => `
      <div class="secret-field-dropdown-item" data-ref="${escHtml(r.ref)}"
           style="padding:8px 10px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px solid var(--border-dim);font-size:12px">
        <code style="color:var(--info)">${escHtml(r.ref)}</code>
        <span style="color:var(--text-dim);font-family:var(--font-mono);font-size:10px">${escHtml(r.hint || '')}</span>
      </div>
    `).join('');
    dropdown.querySelectorAll('.secret-field-dropdown-item').forEach(item => {
      item.addEventListener('mouseenter', () => { item.style.background = 'var(--bg-hover)'; });
      item.addEventListener('mouseleave', () => { item.style.background = ''; });
      item.addEventListener('click', () => {
        const ref = item.dataset.ref;
        setValue(ref);
        closeDropdown();
      });
    });
  }

  async function openDropdown() {
    // Always force-refresh on open — stale cache would hide secrets created
    // in a different view (wizard, Secrets page, another SecretField instance)
    // after this instance was mounted. The API is cheap; the UX cost of
    // "my new secret isn't here" is not.
    const refs = await loadRefs(true);
    renderDropdown(refs);
    dropdown.hidden = false;
    state.dropdownOpen = true;
    setTimeout(() => document.addEventListener('click', handleOutsideClick), 0);
  }

  function closeDropdown() {
    dropdown.hidden = true;
    state.dropdownOpen = false;
    document.removeEventListener('click', handleOutsideClick);
  }

  function handleOutsideClick(e) {
    if (!root.contains(e.target)) closeDropdown();
  }

  function renderMode() {
    if (state.ref) {
      input.style.display = 'none';
      pill.style.display = 'inline-flex';
      pill.hidden = false;
      pillName.textContent = state.ref;
    } else {
      input.style.display = '';
      pill.hidden = true;
      pill.style.display = 'none';
      input.value = state.value || '';
    }
    updateStatus();
  }

  function setValue(v) {
    if (v == null) v = '';
    state.value = v;
    state.ref = v.startsWith('$') ? v : null;
    renderMode();
  }

  input.addEventListener('input', () => {
    state.value = input.value;
    state.ref = input.value.startsWith('$') ? input.value : null;
    if (state.ref) {
      renderMode();
    } else {
      updateStatus();
    }
  });

  pillClear.addEventListener('click', () => {
    setValue('');
    setTimeout(() => input.focus(), 0);
  });

  dropdownBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (state.dropdownOpen) closeDropdown();
    else openDropdown();
  });

  // Initial render
  renderMode();

  return {
    getValue() { return state.value || ''; },
    setValue,
    setContext(newContext) {
      state.context = newContext;
      updateStatus();
    },
    setPrefix(newPrefix) {
      state.prefix = newPrefix;
      updateStatus();
    },
    focus() { if (!state.ref) input.focus(); },
    destroy() {
      closeDropdown();
      container.innerHTML = '';
    },
  };
}
