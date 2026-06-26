/**
 * MCP view — dedicated, focused MCP server management + tool inventory.
 *
 * Reuses the Settings "MCP" panel renderer. Renders NO Settings tab strip: a
 * sidebar drill-down shows only the panel the user asked for.
 */

import { renderMCP } from './settings.js';

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">MCP Servers</h2>
    </div>
    <div id="mcp-panel"></div>
  `;
  await renderMCP(document.getElementById('mcp-panel'));
}
