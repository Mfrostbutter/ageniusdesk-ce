/**
 * Models view — dedicated, focused AI model + instruction configuration.
 *
 * Reuses the Settings "AI Settings" panel renderer. Renders NO Settings tab
 * strip: a sidebar drill-down shows only the panel the user asked for.
 */

import { renderModelsTab } from './settings.js';

export async function render(container) {
  container.innerHTML = `
    <div class="section-header">
      <h2 class="section-title">AI Models</h2>
    </div>
    <div id="models-panel"></div>
  `;
  await renderModelsTab(document.getElementById('models-panel'));
}
