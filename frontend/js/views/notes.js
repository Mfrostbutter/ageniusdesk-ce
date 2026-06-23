/**
 * Notes view — 3-pane Obsidian-style vault browser.
 *
 * Left:  folder tree + tag list + search box
 * Mid:   CodeMirror 6 markdown editor (ESM from esm.sh)
 * Right: backlinks for the active note
 *
 * CodeMirror loads lazily — first visit incurs one network fetch, subsequent
 * navigations reuse the cached module graph. No build step required.
 */

import { get, put, post, del } from '../api.js';
import * as toast from '../components/toast.js';

// esm.sh serves ESM modules off npm. The `codemirror` metapackage re-exports
// basicSetup + core APIs so we don't have to pin every sub-package version
// individually (each has its own unrelated version number — there's no
// "CodeMirror 6.34" suite release). Sub-packages are pulled via esm.sh's
// graph resolver transitively.
const CDN = 'https://esm.sh';

let cmState = null;           // active CodeMirror state object
let cmView = null;            // active CodeMirror view
let currentPath = null;       // vault-relative path of open note
let dirty = false;            // unsaved edits flag
let saveTimer = null;         // debounced autosave handle
let cmModules = null;         // cached CodeMirror module bundle

export async function render(container) {
  container.innerHTML = `
   <div style="height:100%;display:flex;flex-direction:column;min-height:0">
    <div class="section-header" style="margin-bottom:12px;flex:none">
      <h2 class="section-title">Vault</h2>
      <div style="display:flex;align-items:center;gap:8px">
        <span id="notes-status" style="font-size:11px;color:var(--text-dim)"></span>
        <button class="btn btn-sm btn-ghost" id="notes-reindex" title="Rebuild search index from disk">Reindex</button>
        <button class="btn btn-sm btn-primary" id="notes-new">+ New</button>
      </div>
    </div>

    <div id="notes-shell" style="display:grid;grid-template-columns:260px 1fr 240px;gap:12px;flex:1;min-height:0">
      <!-- Left pane: search + tree + tags -->
      <aside style="display:flex;flex-direction:column;gap:10px;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:10px;overflow:hidden">
        <input id="notes-search" type="search" placeholder="Search notes…"
               style="width:100%;background:var(--bg-primary);border:1px solid var(--border-dim);border-radius:6px;padding:6px 8px;color:var(--text-primary);font-size:12px">
        <div id="notes-results" style="font-size:12px;max-height:220px;overflow:auto;display:none"></div>
        <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.5px;margin-top:4px">FILES</div>
        <div id="notes-tree" style="flex:1;overflow:auto;font-size:12px"></div>
        <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.5px;margin-top:4px">TAGS</div>
        <div id="notes-tags" style="max-height:140px;overflow:auto;font-size:12px"></div>
      </aside>

      <!-- Middle pane: editor -->
      <main style="display:flex;flex-direction:column;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);overflow:hidden">
        <div id="notes-titlebar" style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid var(--border-dim);min-height:38px">
          <span id="notes-path" style="font-family:var(--font-mono);font-size:12px;color:var(--text-dim)">(no note selected)</span>
          <div style="display:flex;gap:6px">
            <button class="btn btn-sm btn-ghost" id="notes-save" title="Save (Cmd+S)">Save</button>
            <button class="btn btn-sm btn-ghost btn-danger" id="notes-delete" title="Archive this note">Archive</button>
          </div>
        </div>
        <div id="notes-editor" style="flex:1;overflow:auto"></div>
      </main>

      <!-- Right pane: backlinks + metadata -->
      <aside style="display:flex;flex-direction:column;gap:10px;background:var(--bg-input);border:1px solid var(--border-dim);border-radius:var(--radius);padding:10px;overflow:auto">
        <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.5px">BACKLINKS</div>
        <div id="notes-backlinks" style="font-size:12px"></div>
        <div style="font-size:10px;color:var(--text-dim);letter-spacing:0.5px;margin-top:8px">TAGS ON THIS NOTE</div>
        <div id="notes-this-tags" style="font-size:12px;display:flex;flex-wrap:wrap;gap:4px"></div>
      </aside>
    </div>
   </div>
  `;

  document.getElementById('notes-search').addEventListener('input', debounce(onSearch, 200));
  document.getElementById('notes-reindex').addEventListener('click', onReindex);
  document.getElementById('notes-new').addEventListener('click', onNew);
  document.getElementById('notes-save').addEventListener('click', () => saveCurrent());
  document.getElementById('notes-delete').addEventListener('click', onDelete);

  // Cmd+S anywhere in the view saves
  container.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 's') {
      e.preventDefault();
      saveCurrent();
    }
  });

  await Promise.all([loadTree(), loadTags()]);
}

// ── Tree + tags ─────────────────────────────────────────────────────────────

async function loadTree() {
  try {
    const tree = await get('/api/notes/tree');
    renderTree(document.getElementById('notes-tree'), tree, '');
  } catch (e) {
    document.getElementById('notes-tree').innerHTML = `<div style="color:var(--error)">Tree load failed: ${escape(e.message)}</div>`;
  }
}

function renderTree(container, node, parentPath) {
  container.innerHTML = '';
  const children = node.children || [];
  children.sort((a, b) => {
    if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const child of children) {
    const path = parentPath ? `${parentPath}/${child.name}` : child.name;
    const row = document.createElement('div');
    row.style.cssText = 'padding:3px 6px;cursor:pointer;border-radius:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
    if (child.type === 'dir') {
      row.textContent = `📁 ${child.name}`;
      row.addEventListener('click', () => {
        if (row.dataset.expanded) {
          row.nextElementSibling.remove();
          delete row.dataset.expanded;
        } else {
          const inner = document.createElement('div');
          inner.style.cssText = 'padding-left:10px';
          renderTree(inner, child, path);
          row.after(inner);
          row.dataset.expanded = '1';
        }
      });
    } else {
      row.textContent = `📄 ${child.name}`;
      row.dataset.path = path;
      row.addEventListener('click', () => openNote(path));
      row.addEventListener('mouseenter', () => { row.style.background = 'var(--bg-primary)'; });
      row.addEventListener('mouseleave', () => { row.style.background = currentPath === path ? 'var(--bg-primary)' : ''; });
    }
    container.appendChild(row);
  }
}

async function loadTags() {
  try {
    const { tags } = await get('/api/notes/tags');
    const el = document.getElementById('notes-tags');
    if (!tags.length) { el.innerHTML = '<div style="color:var(--text-dim)">(no tags yet)</div>'; return; }
    el.innerHTML = '';
    for (const { tag, count } of tags) {
      const pill = document.createElement('span');
      pill.className = 'pill';
      pill.style.cssText = 'display:inline-block;margin:2px;cursor:pointer;font-size:11px';
      pill.textContent = `#${tag} · ${count}`;
      pill.addEventListener('click', () => {
        document.getElementById('notes-search').value = '';
        searchByTag(tag);
      });
      el.appendChild(pill);
    }
  } catch {}
}

// ── Search ──────────────────────────────────────────────────────────────────

async function onSearch(e) {
  const q = e.target.value.trim();
  const results = document.getElementById('notes-results');
  if (!q) { results.style.display = 'none'; return; }
  const r = await get(`/api/notes/search?q=${encodeURIComponent(q)}`).catch(() => ({ results: [] }));
  renderSearchResults(r.results);
}

async function searchByTag(tag) {
  const r = await get(`/api/notes/search?tag=${encodeURIComponent(tag)}`).catch(() => ({ results: [] }));
  renderSearchResults(r.results);
}

function renderSearchResults(items) {
  const el = document.getElementById('notes-results');
  if (!items.length) { el.innerHTML = '<div style="color:var(--text-dim);padding:4px 6px">(no matches)</div>'; el.style.display = 'block'; return; }
  el.innerHTML = '';
  for (const item of items) {
    const row = document.createElement('div');
    row.style.cssText = 'padding:6px 6px;cursor:pointer;border-bottom:1px solid var(--border-dim)';
    row.innerHTML = `<div style="font-weight:500">${escape(item.title || item.path)}</div>
                     <div style="color:var(--text-dim);font-size:11px;font-family:var(--font-mono)">${escape(item.path)}</div>
                     ${item.snippet ? `<div style="color:var(--text-secondary);font-size:11px;margin-top:2px">${item.snippet}</div>` : ''}`;
    row.addEventListener('click', () => openNote(item.path));
    el.appendChild(row);
  }
  el.style.display = 'block';
}

// ── Editor ──────────────────────────────────────────────────────────────────

async function ensureCodeMirror() {
  if (cmModules) return cmModules;
  // The `codemirror` metapackage exports basicSetup + EditorView; EditorState
  // lives in @codemirror/state. Import each piece from its canonical package
  // and let esm.sh dedupe the graph.
  const [meta, state, view, commands, md] = await Promise.all([
    import(`${CDN}/codemirror`),
    import(`${CDN}/@codemirror/state`),
    import(`${CDN}/@codemirror/view`),
    import(`${CDN}/@codemirror/commands`),
    import(`${CDN}/@codemirror/lang-markdown`),
  ]);
  cmModules = { meta, state, view, commands, md };
  return cmModules;
}

async function mountEditor(content) {
  const { meta, state, view, commands, md } = await ensureCodeMirror();
  const { EditorState } = state;
  const { EditorView, keymap } = view;
  const { basicSetup } = meta;
  const { defaultKeymap, history, historyKeymap, indentWithTab } = commands;
  const { markdown } = md;

  const host = document.getElementById('notes-editor');
  host.innerHTML = '';

  const listener = EditorView.updateListener.of((update) => {
    if (update.docChanged) {
      dirty = true;
      setStatus('unsaved');
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => { if (dirty) saveCurrent({ silent: true }); }, 1500);
    }
  });

  cmState = EditorState.create({
    doc: content,
    extensions: [
      // basicSetup bundles line numbers, history, search, brackets, etc.
      // — the standard "sensible editor" defaults CodeMirror ships for this
      // exact use case.
      basicSetup,
      markdown(),
      listener,
      EditorView.lineWrapping,
      EditorView.theme({
        '&': { height: '100%', fontSize: '13px', background: 'var(--bg-input)' },
        '.cm-content': { fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', caretColor: 'var(--accent)' },
        '.cm-gutters': { background: 'var(--bg-input)', color: 'var(--text-dim)', borderRight: '1px solid var(--border-dim)' },
        '.cm-activeLine': { background: 'rgba(255,255,255,0.03)' },
        '.cm-activeLineGutter': { background: 'rgba(255,255,255,0.05)' },
      }, { dark: true }),
      keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
    ],
  });

  cmView = new EditorView({ state: cmState, parent: host });
}

async function openNote(path) {
  if (dirty && currentPath && currentPath !== path) {
    if (!confirm(`Unsaved changes in ${currentPath}. Discard?`)) return;
  }
  try {
    const { content } = await get(`/api/notes/${encodeURI(path)}`);
    currentPath = path;
    dirty = false;
    document.getElementById('notes-path').textContent = path;
    if (cmView) {
      cmView.dispatch({ changes: { from: 0, to: cmView.state.doc.length, insert: content } });
      dirty = false;
    } else {
      await mountEditor(content);
    }
    setStatus('loaded');
    loadBacklinks(path);
  } catch (e) {
    toast.error(`Couldn't open ${path}: ${e.message}`);
  }
}

async function saveCurrent({ silent = false } = {}) {
  if (!currentPath || !cmView) return;
  const content = cmView.state.doc.toString();
  try {
    const meta = await put(`/api/notes/${encodeURI(currentPath)}`, { content });
    dirty = false;
    setStatus('saved');
    if (!silent) toast.success('Saved');
    renderMeta(meta);
    loadTags();
  } catch (e) {
    toast.error(`Save failed: ${e.message}`);
    setStatus('error');
  }
}

async function onNew() {
  const path = prompt('New note path (e.g. user/idea.md):', 'user/new-note.md');
  if (!path) return;
  try {
    await put(`/api/notes/${encodeURI(path)}`, { content: `# ${path.split('/').pop().replace(/\.md$/, '')}\n\n` });
    await loadTree();
    openNote(path.endsWith('.md') ? path : path + '.md');
  } catch (e) { toast.error(e.message); }
}

async function onDelete() {
  if (!currentPath) return;
  if (!confirm(`Archive "${currentPath}"? It's moved to .archive/, not deleted.`)) return;
  try {
    await del(`/api/notes/${encodeURI(currentPath)}`);
    toast.success('Archived');
    currentPath = null;
    dirty = false;
    document.getElementById('notes-path').textContent = '(no note selected)';
    if (cmView) cmView.dispatch({ changes: { from: 0, to: cmView.state.doc.length, insert: '' } });
    document.getElementById('notes-backlinks').innerHTML = '';
    document.getElementById('notes-this-tags').innerHTML = '';
    await loadTree();
  } catch (e) { toast.error(e.message); }
}

async function onReindex() {
  setStatus('reindexing');
  try {
    const r = await post('/api/notes/reindex', {});
    toast.success(`Reindexed ${r.indexed} notes`);
    await Promise.all([loadTree(), loadTags()]);
    setStatus('ready');
  } catch (e) { toast.error(e.message); setStatus('error'); }
}

// ── Right pane ──────────────────────────────────────────────────────────────

async function loadBacklinks(path) {
  try {
    const { backlinks } = await get(`/api/notes/${encodeURI(path)}/backlinks`);
    const el = document.getElementById('notes-backlinks');
    if (!backlinks.length) { el.innerHTML = '<div style="color:var(--text-dim)">(none)</div>'; return; }
    el.innerHTML = '';
    for (const b of backlinks) {
      const row = document.createElement('div');
      row.style.cssText = 'padding:4px 0;cursor:pointer';
      row.innerHTML = `<span style="font-weight:500">${escape(b.title || b.path)}</span><br><span style="color:var(--text-dim);font-size:10px;font-family:var(--font-mono)">${escape(b.path)}</span>`;
      row.addEventListener('click', () => openNote(b.path));
      el.appendChild(row);
    }
  } catch {}
}

function renderMeta(meta) {
  const el = document.getElementById('notes-this-tags');
  if (!el) return;
  el.innerHTML = '';
  for (const tag of meta.tags || []) {
    const s = document.createElement('span');
    s.className = 'pill';
    s.style.cssText = 'font-size:11px';
    s.textContent = `#${tag}`;
    el.appendChild(s);
  }
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function setStatus(s) {
  const el = document.getElementById('notes-status');
  if (!el) return;
  const colors = { loaded: 'var(--text-dim)', unsaved: 'var(--warning)', saved: 'var(--success)', reindexing: 'var(--accent)', error: 'var(--error)', ready: 'var(--text-dim)' };
  el.textContent = s;
  el.style.color = colors[s] || 'var(--text-dim)';
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function escape(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}
