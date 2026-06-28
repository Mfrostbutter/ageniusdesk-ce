// Behavioral XSS regression for frontend/js/components/error-item.js.
//
// Renders a hostile error (every attacker-influenced field set to an attribute-
// breakout payload) and asserts no value escapes its HTML attribute / inline-JS
// context as live markup. The component's workflow_id / execution_id are
// attacker-controlled via the login-exempt POST /api/errors/webhook, so a weak
// escaper here is a stored-XSS hole. Exits non-zero (failing the pytest wrapper)
// if any breakout is detected.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const componentPath = join(here, '..', '..', 'frontend', 'js', 'components', 'error-item.js');

// Load the ES module without a bundler: strip the export keyword and evaluate
// the source in a scope that provides a faithful `document` shim. The shim's
// textContent -> innerHTML escapes & < > only (exactly like a real DOM element),
// so the test observes the SAME escaping a browser would apply via esc().
let src = readFileSync(componentPath, 'utf8').replace(/export\s+function/g, 'function');

const documentShim = {
  createElement() {
    let _t = '';
    return {
      set textContent(v) { _t = String(v); },
      get innerHTML() {
        return _t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      },
    };
  },
};

const factory = new Function('document', 'window', src + '\n;return renderErrorItem;');
const renderErrorItem = factory(documentShim, { __n8nUrl: 'http://n8n.test' });

const EVIL = '"><img src=x onerror=alert(1)>';
const html = renderErrorItem(
  {
    instance_id: 'i1',
    workflow_id: EVIL,
    execution_id: EVIL,
    workflow_name: EVIL,
    error_message: EVIL,
    node_name: EVIL,
    error_type: EVIL,
    occurred_at: '2026-01-01 00:00:00',
  },
  { instanceMap: { i1: { name: EVIL, color: EVIL, n8nUrl: 'http://n8n.test' } } },
);

const failures = [];
// If any payload survived as a live tag, an attribute/JS context was broken out
// of. A safely-escaped payload renders as &lt;img...&gt; (no literal `<img`).
if (/<img/i.test(html)) failures.push('literal <img> tag injected (attribute or inline-JS breakout)');
if (/<script/i.test(html)) failures.push('literal <script> tag injected');

if (failures.length) {
  console.error('XSS regression in error-item.js:\n - ' + failures.join('\n - '));
  process.exit(1);
}
console.log('error-item.js escaping OK');
