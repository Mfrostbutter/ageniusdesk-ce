/**
 * Instances view — dedicated, focused n8n instance management.
 *
 * Reuses the Settings "Instances" panel renderer so the gear-Settings tab and
 * this standalone sidebar view stay in lockstep. Deliberately renders NO
 * Settings tab strip: a sidebar drill-down should show only what the user
 * clicked, not the whole Settings surface.
 */

import { renderInstances } from './settings.js';

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">n8n Instances</h2>
    </div>
    <div id="instances-panel"></div>
  `;
  await renderInstances(document.getElementById('instances-panel'));
}
