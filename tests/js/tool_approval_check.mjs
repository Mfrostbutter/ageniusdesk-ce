// Behavioral checks for frontend/js/components/tool-approval.js.
//
// The approval card is the operator's only view of what the assistant wants to
// run, and every field on it originates with the MODEL: the tool name and the
// arguments come back from an LLM that may itself be acting on text injected
// into an n8n error payload or a RAG hit. So the card renders hostile input by
// construction, and two properties have to hold:
//
//   1. No field escapes its HTML context as live markup. Otherwise the injection
//      that reached the model also lands as XSS in the operator's session.
//   2. The card never renders a Run button for a proposal without its id, and
//      shows the arguments verbatim (escaped), so the operator approves what
//      would actually run rather than a summary they can be fooled by.
//
// Exits non-zero (failing the pytest wrapper) on any breakout.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const componentPath = join(here, '..', '..', 'frontend', 'js', 'components', 'tool-approval.js');

// Load the ES module without a bundler: drop the import lines (api.js / toast.js
// are only needed on click, which this harness never exercises) and strip the
// export keywords so the functions land in the evaluated scope.
let src = readFileSync(componentPath, 'utf8')
  .replace(/^import[^;]+;$/gm, '')
  .replace(/export\s+function/g, 'function');

const factory = new Function('post', 'toast', 'window', src + '\n;return { renderPendingActions, hasPendingActions };');
const { renderPendingActions, hasPendingActions } = factory(
  async () => ({}),
  { error() {} },
  { dispatchEvent() {} },
);

const failures = [];
const EVIL = '"><img src=x onerror=alert(1)>';

// 1. Every string field on a proposal carries the payload.
const html = renderPendingActions([{
  id: EVIL,
  tool: EVIL,
  is_mcp: true,
  server_id: EVIL,
  reasoning: EVIL,
  arguments: { [EVIL]: EVIL, nested: { deep: EVIL } },
}]);

if (/<img/i.test(html)) failures.push('literal <img> tag injected (attribute or text breakout)');
if (/<script/i.test(html)) failures.push('literal <script> tag injected');
if (/onerror=/i.test(html) && !/onerror=alert\(1\)&gt;/i.test(html)) {
  failures.push('unescaped onerror= handler survived');
}

// 2. The operator must see the real arguments, not a summary. The payload has to
//    be PRESENT (escaped) rather than dropped, or the card would hide what runs.
if (!html.includes('&lt;img src=x onerror=alert(1)&gt;')) {
  failures.push('argument payload was not rendered escaped — card may be hiding what would run');
}

// 3. An approve button must always be tied to an id.
if (/data-pending-act="confirm"/.test(html) && !/data-pending-id="/.test(html)) {
  failures.push('Run button rendered without a proposal id');
}

// 4. Empty / absent pending lists render nothing at all (no stray chrome).
for (const empty of [undefined, null, []]) {
  if (renderPendingActions(empty) !== '') {
    failures.push(`renderPendingActions(${JSON.stringify(empty)}) should render an empty string`);
  }
}

// 5. hasPendingActions is what the no-card surfaces branch on, so it must not
//    report false for a real proposal.
if (hasPendingActions({ pending_actions: [{ id: 'a', tool: 't' }] }) !== true) {
  failures.push('hasPendingActions missed a real proposal');
}
for (const none of [{}, { pending_actions: [] }, { pending_actions: null }, undefined]) {
  if (hasPendingActions(none) !== false) {
    failures.push(`hasPendingActions(${JSON.stringify(none)}) should be false`);
  }
}

if (failures.length) {
  console.error('tool-approval.js check failed:\n - ' + failures.join('\n - '));
  process.exit(1);
}
console.log('tool-approval.js escaping + contract OK');
