/**
 * Toast notification system.
 */

const container = document.getElementById('toast-container');

export function toast(message, type = 'info', duration = 4000) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 200); }, duration);
}

export function success(msg) { toast(msg, 'success'); }
export function error(msg) { toast(msg, 'error'); }
export function warning(msg) { toast(msg, 'warning'); }
export function info(msg) { toast(msg, 'info'); }
