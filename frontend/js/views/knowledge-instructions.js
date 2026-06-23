/**
 * Knowledge -> Instructions -- Baseline Constitution editor (C3).
 *
 * Talks to GET/PUT /api/assistant/baseline.  The document is the operator-
 * authored "house rules" prepended to every agent's system prompt.
 *
 * Version tracking: every GET returns the current version; every PUT sends
 * back expected_version and gets a 409 if another tab saved in the meantime.
 */

import * as toast from '../components/toast.js';

let _currentVersion = null;
let _currentContent = '';
let _dirty = false;

export async function render(container) {
  container.innerHTML = `
    <div class="section-header" style="margin-bottom:16px">
      <div>
        <h2 class="section-title">Agent Constitution</h2>
        <span class="card-subtitle" id="ki-meta">Baseline rules prepended to every agent's system prompt</span>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span id="ki-saved-indicator" style="font-size:12px;color:var(--text-dim);opacity:0;transition:opacity 0.3s">Saved</span>
        <button class="btn btn-sm btn-primary" id="ki-save" disabled>Save</button>
      </div>
    </div>

    <div id="ki-disabled-notice" style="display:none">
      <div class="card" style="padding:24px;text-align:center;color:var(--text-secondary)">
        <p style="margin:0 0 8px;font-weight:600">Constitution disabled</p>
        <p style="margin:0;font-size:13px">
          Set <code style="font-family:var(--font-mono);font-size:12px">AGD_CONSTITUTION_ENABLED=true</code>
          to enable this feature.
        </p>
      </div>
    </div>

    <div id="ki-main">
      <div class="card" style="margin-bottom:16px">
        <div style="padding:12px 16px;color:var(--text-secondary);font-size:13px;line-height:1.6;border-bottom:1px solid var(--border-dim)">
          This document is prepended to every agent's system prompt before per-agent text.
          Use H2 headings (<code style="font-family:var(--font-mono);font-size:12px">## Section Name</code>)
          as override anchors. Mark sections as overrideable below so per-agent prompts can replace them.
        </div>
        <div style="padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <label style="font-size:12px;color:var(--text-secondary);white-space:nowrap">
            Overrideable sections (comma-separated slugs):
          </label>
          <input
            id="ki-sections"
            type="text"
            placeholder="tone, tools"
            style="flex:1;min-width:200px;padding:6px 10px;font-family:var(--font-mono);font-size:12px;background:var(--bg-input);border:1px solid var(--border-mid);border-radius:6px;color:var(--text-primary);outline:none"
          />
        </div>
      </div>

      <div class="card" style="display:flex;flex-direction:column">
        <div id="ki-editor-wrap" style="display:flex;flex-direction:column;height:calc(100vh - 340px);min-height:400px">
          <textarea
            id="ki-editor"
            spellcheck="false"
            style="flex:1;padding:16px;font-family:var(--font-mono);font-size:13px;line-height:1.65;background:var(--bg-input);color:var(--text-primary);border:none;resize:none;outline:none;width:100%;box-sizing:border-box;border-radius:0 0 8px 8px"
            placeholder="Loading..."
          ></textarea>
        </div>
      </div>

      <div id="ki-size-warning" style="display:none;margin-top:10px;padding:8px 12px;border-radius:var(--radius);background:var(--warning-glow);color:var(--warning);border-left:3px solid var(--warning);font-size:12px">
        Constitution is getting long -- consider whether all of this is universally applicable.
        Sections that vary per agent belong in per-agent prompts, not here.
      </div>
      <div style="margin-top:12px;color:var(--text-dim);font-size:11px;display:flex;gap:16px;flex-wrap:wrap">
        <span>Markdown supported. H2 headings are override anchors.</span>
        <span id="ki-char-count"></span>
        <span>Saved to <code style="font-family:var(--font-mono);font-size:11px">data/baseline/baseline.md</code> on the server</span>
      </div>
    </div>
  `;

  const editor = container.querySelector('#ki-editor');
  const saveBtn = container.querySelector('#ki-save');
  const charCount = container.querySelector('#ki-char-count');
  const sectionsInput = container.querySelector('#ki-sections');

  function markDirty() {
    _dirty = true;
    saveBtn.disabled = false;
  }

  editor.addEventListener('input', () => {
    markDirty();
    const bytes = new TextEncoder().encode(editor.value).length;
    if (charCount) charCount.textContent = `${bytes} bytes`;
    const sizeWarn = container.querySelector('#ki-size-warning');
    if (sizeWarn) sizeWarn.style.display = bytes > 8192 ? '' : 'none';
  });

  sectionsInput.addEventListener('input', () => {
    markDirty();
  });

  saveBtn.addEventListener('click', () => _save(container));

  editor.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 's') {
      e.preventDefault();
      if (!saveBtn.disabled) _save(container);
    }
  });

  await _load(container);
}

async function _load(container) {
  const editor = container.querySelector('#ki-editor');
  const charCount = container.querySelector('#ki-char-count');
  const sectionsInput = container.querySelector('#ki-sections');
  const meta = container.querySelector('#ki-meta');
  const disabledNotice = container.querySelector('#ki-disabled-notice');
  const main = container.querySelector('#ki-main');

  try {
    const resp = await fetch('/api/assistant/baseline');
    if (resp.status === 503) {
      disabledNotice.style.display = '';
      main.style.display = 'none';
      return;
    }
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }
    const data = await resp.json();

    _currentVersion = data.version;
    _currentContent = data.content || '';
    _dirty = false;

    editor.value = _currentContent;
    sectionsInput.value = (data.overrideable_sections || []).join(', ');

    if (meta && data.version && data.updated) {
      meta.textContent = `v${data.version} · last saved ${data.updated}`;
    }
    const loadBytes = new TextEncoder().encode(_currentContent).length;
    if (charCount) charCount.textContent = `${loadBytes} bytes`;
    const sizeWarnEl = container.querySelector('#ki-size-warning');
    if (sizeWarnEl) sizeWarnEl.style.display = loadBytes > 8192 ? '' : 'none';

    const saveBtn = container.querySelector('#ki-save');
    if (saveBtn) saveBtn.disabled = true;
  } catch (e) {
    if (editor) editor.value = '';
    toast.error('Failed to load constitution: ' + (e.message || e));
  }
}

async function _save(container) {
  const editor = container.querySelector('#ki-editor');
  const sectionsInput = container.querySelector('#ki-sections');
  const saveBtn = container.querySelector('#ki-save');
  const indicator = container.querySelector('#ki-saved-indicator');
  const meta = container.querySelector('#ki-meta');
  const charCount = container.querySelector('#ki-char-count');

  if (_currentVersion === null) {
    toast.error('Cannot save: version not loaded yet. Reload the page.');
    return;
  }

  const content = editor.value;
  const rawSections = sectionsInput.value;
  const overrideable_sections = rawSections
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);

  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving...';

  try {
    const resp = await fetch('/api/assistant/baseline', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_version: _currentVersion,
        overrideable_sections,
        content,
      }),
    });

    if (resp.status === 409) {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
      toast.error('Constitution was modified elsewhere -- reload to merge.');
      return;
    }
    if (resp.status === 413) {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
      toast.error('Constitution body too large (max 64 KiB).');
      return;
    }
    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }

    const data = await resp.json();
    _currentVersion = data.version;
    _currentContent = content;
    _dirty = false;

    saveBtn.textContent = 'Save';

    if (meta && data.version && data.updated) {
      meta.textContent = `v${data.version} · last saved ${data.updated}`;
    }
    if (charCount) charCount.textContent = `${new TextEncoder().encode(content).length} bytes`;

    if (indicator) {
      indicator.style.opacity = '1';
      setTimeout(() => { indicator.style.opacity = '0'; }, 2000);
    }
    toast.success(`Constitution saved (v${data.version})`);
  } catch (e) {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save';
    toast.error('Save failed: ' + (e.message || e));
  }
}

export function teardown() {
  _dirty = false;
  _currentVersion = null;
  _currentContent = '';
}
