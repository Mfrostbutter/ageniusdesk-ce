/**
 * Theme loader — fetches theme JSON and applies CSS custom properties.
 */

import { get, post } from './api.js';
import { startMatrixRain, stopMatrixRain } from './components/matrix-rain.js';

let currentTheme = 'dark';

export async function loadTheme(themeId) {
  try {
    const theme = await get(`/api/themes/${themeId}`);
    applyTheme(theme);
    currentTheme = themeId;
  } catch {
    // Fall back silently — default CSS vars still work
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;

  if (theme.colors) {
    for (const [key, value] of Object.entries(theme.colors)) {
      root.style.setProperty(`--${key}`, value);
    }
  }

  if (theme.fonts) {
    if (theme.fonts.body) root.style.setProperty('--font-body', `'${theme.fonts.body}', sans-serif`);
    if (theme.fonts.mono) root.style.setProperty('--font-mono', `'${theme.fonts.mono}', monospace`);
  }

  // Theme-driven matrix rain — start when the theme requests it, stop otherwise
  if (theme.effects?.['matrix-rain']) {
    startMatrixRain();
  } else {
    stopMatrixRain();
  }
}

export async function setActiveTheme(themeId) {
  await post(`/api/themes/active/${themeId}`);
  await loadTheme(themeId);
}

export function getCurrentTheme() {
  return currentTheme;
}
