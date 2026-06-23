/**
 * Theme-driven Matrix Rain background.
 * Started/stopped by themes.js when the active theme requests it.
 */

let mrCanvas = null;
let mrCtx = null;
let mrColumns = [];
let mrRafId = null;
let mrResizeObserver = null;
const MR_FONT_SIZE = 14;
const MR_CHARS = 'アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン0123456789ABCDEF';

function mrInit() {
  if (!mrCanvas) return;
  const cols = Math.floor(mrCanvas.width / MR_FONT_SIZE);
  mrColumns = [];
  for (let i = 0; i < cols; i++) {
    mrColumns.push(Math.random() * -(mrCanvas.height / MR_FONT_SIZE));
  }
}

function mrResize() {
  if (!mrCanvas) return;
  const w = window.innerWidth - (parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width')) || 220);
  const h = window.innerHeight;
  if (w <= 0 || h <= 0) return;
  mrCanvas.width = w;
  mrCanvas.height = h;
  if (mrCtx) {
    mrCtx.fillStyle = '#000000';
    mrCtx.fillRect(0, 0, w, h);
  }
  mrInit();
}

function mrTick() {
  if (!mrCtx || !mrCanvas) return;

  mrCtx.fillStyle = 'rgba(0, 0, 0, 0.045)';
  mrCtx.fillRect(0, 0, mrCanvas.width, mrCanvas.height);

  mrCtx.font = `${MR_FONT_SIZE}px "JetBrains Mono", monospace`;

  for (let i = 0; i < mrColumns.length; i++) {
    const y = mrColumns[i];
    const x = i * MR_FONT_SIZE;
    const headY = Math.floor(y);

    mrCtx.fillStyle = 'rgba(200, 255, 214, 0.95)';
    const headChar = MR_CHARS[Math.floor(Math.random() * MR_CHARS.length)];
    mrCtx.fillText(headChar, x, headY * MR_FONT_SIZE);

    if (headY > 1) {
      mrCtx.fillStyle = 'rgba(0, 255, 65, 0.55)';
      const bodyChar = MR_CHARS[Math.floor(Math.random() * MR_CHARS.length)];
      mrCtx.fillText(bodyChar, x, (headY - 1) * MR_FONT_SIZE);
    }

    mrColumns[i] += 0.4 + Math.random() * 0.35;
    if (mrColumns[i] * MR_FONT_SIZE > mrCanvas.height && Math.random() > 0.975) {
      mrColumns[i] = Math.random() * -20;
    }
  }

  mrRafId = requestAnimationFrame(mrTick);
}

export function startMatrixRain() {
  stopMatrixRain();

  mrCanvas = document.createElement('canvas');
  mrCanvas.id = 'matrix-rain-canvas';
  mrCanvas.style.cssText = [
    'position:fixed',
    'top:0',
    'left:var(--sidebar-width,220px)',
    'width:calc(100vw - var(--sidebar-width,220px))',
    'height:100vh',
    'pointer-events:none',
    'z-index:-1',
    'display:block',
  ].join(';');
  document.body.prepend(mrCanvas);

  mrCtx = mrCanvas.getContext('2d');

  const sidebarWidth = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width')) || 220;
  const w = window.innerWidth - sidebarWidth;
  const h = window.innerHeight;
  mrCanvas.width = w;
  mrCanvas.height = h;
  mrCtx.fillStyle = '#000000';
  mrCtx.fillRect(0, 0, w, h);
  mrInit();
  if (mrRafId === null) {
    mrRafId = requestAnimationFrame(mrTick);
  }

  mrResizeObserver = new ResizeObserver(() => mrResize());
  mrResizeObserver.observe(document.body);
}

export function stopMatrixRain() {
  if (mrRafId !== null) {
    cancelAnimationFrame(mrRafId);
    mrRafId = null;
  }
  if (mrResizeObserver) {
    mrResizeObserver.disconnect();
    mrResizeObserver = null;
  }
  document.getElementById('matrix-rain-canvas')?.remove();
  mrCanvas = null;
  mrCtx = null;
  mrColumns = [];
}
