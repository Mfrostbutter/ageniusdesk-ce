/**
 * Modal helpers — show/hide pre-rendered modals, plus an ad-hoc themed
 * confirm dialog with a type-to-match input for destructive actions,
 * plus a general-purpose themed confirm/inform dialog.
 */

export function show(id) {
  document.getElementById(id)?.classList.remove('hidden');
}

export function hide(id) {
  document.getElementById(id)?.classList.add('hidden');
}

/**
 * General-purpose themed modal dialog. Returns a Promise<boolean> that
 * resolves true on confirm, false on cancel/dismiss.
 *
 * @param {object} opts
 * @param {string}             opts.title          - Heading text (plain string, HTML-escaped internally)
 * @param {string|HTMLElement} opts.body           - Body content: plain string or an HTMLElement
 * @param {string}             [opts.confirmLabel] - Confirm button label (default "OK")
 * @param {string}             [opts.cancelLabel]  - Cancel button label (default "Cancel").
 *                                                   Pass null/empty to hide the cancel button.
 * @param {boolean}            [opts.danger]       - When true, styles the confirm button with --error color
 * @param {HTMLElement}        [opts.triggerEl]    - Element to restore focus to on close
 */
export function openModal({
  title,
  body,
  confirmLabel = 'OK',
  cancelLabel = 'Cancel',
  danger = false,
  triggerEl = null,
}) {
  return new Promise((resolve) => {
    const root = document.createElement('div');
    root.className = 'modal';
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-label', title);

    const confirmStyle = danger
      ? `color:var(--error);border-color:var(--error);background:transparent`
      : ``;

    const cancelHtml = cancelLabel
      ? `<button type="button" class="btn btn-sm" data-modal-action="cancel" style="margin-right:6px">${_esc(cancelLabel)}</button>`
      : '';

    root.innerHTML = `
      <div class="modal-content" tabindex="-1">
        <h2 style="margin-bottom:12px">${_esc(title)}</h2>
        <div id="_modal_body" style="color:var(--text-secondary);font-size:14px;line-height:1.5;margin-bottom:20px"></div>
        <div style="display:flex;justify-content:flex-end;gap:8px">
          ${cancelHtml}
          <button type="button" class="btn btn-sm${danger ? '' : ' btn-primary'}" data-modal-action="confirm" style="${confirmStyle}">${_esc(confirmLabel)}</button>
        </div>
      </div>
    `;

    // Inject body as text or DOM node.
    const bodyEl = root.querySelector('#_modal_body');
    if (body instanceof HTMLElement) {
      bodyEl.appendChild(body);
    } else {
      bodyEl.textContent = body ?? '';
    }

    // ── Focus trap ─────────────────────────────────────────────────────────
    const content = root.querySelector('.modal-content');
    const getFocusables = () => Array.from(
      content.querySelectorAll(
        'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'
      )
    );

    // ── Cleanup ────────────────────────────────────────────────────────────
    const cleanup = (result) => {
      document.removeEventListener('keydown', onKey);
      root.remove();
      if (triggerEl && typeof triggerEl.focus === 'function') triggerEl.focus();
      resolve(result);
    };

    // ── Key handler ─────────────────────────────────────────────────────────
    const onKey = (e) => {
      if (e.key === 'Escape') {
        cleanup(false);
        return;
      }
      if (e.key === 'Enter') {
        // Only confirm on Enter when focus is not inside a form control that
        // uses Enter itself (textarea, select, etc.).
        const tag = document.activeElement?.tagName;
        if (tag !== 'TEXTAREA' && tag !== 'SELECT') {
          cleanup(true);
        }
        return;
      }
      if (e.key === 'Tab') {
        const focusables = getFocusables();
        if (!focusables.length) { e.preventDefault(); return; }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey) {
          if (document.activeElement === first) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    };

    // ── Wire events ─────────────────────────────────────────────────────────
    root.querySelector('[data-modal-action="confirm"]').addEventListener('click', () => cleanup(true));
    const cancelBtn = root.querySelector('[data-modal-action="cancel"]');
    if (cancelBtn) cancelBtn.addEventListener('click', () => cleanup(false));
    root.addEventListener('click', (e) => { if (e.target === root) cleanup(false); });
    document.addEventListener('keydown', onKey);

    document.body.appendChild(root);

    // Focus the confirm button (or first focusable inside body if body has inputs).
    setTimeout(() => {
      const focusables = getFocusables();
      const bodyInputs = body instanceof HTMLElement
        ? Array.from(body.querySelectorAll('input,select,textarea'))
        : [];
      if (bodyInputs.length) {
        bodyInputs[0].focus();
      } else {
        (focusables[0] ?? content).focus();
      }
    }, 0);
  });
}

function _esc(s) {
  const d = document.createElement('span');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

/**
 * Themed type-to-confirm dialog. Resolves true on confirm, false on cancel.
 *
 * @param {object} opts
 * @param {string} opts.title           — heading text
 * @param {string} opts.bodyHtml        — HTML body (caller is responsible for escaping)
 * @param {string} [opts.confirmWord]   — word the user must type (default "DELETE")
 * @param {string} [opts.confirmLabel]  — button label (default "Delete")
 * @param {string} [opts.cancelLabel]   — cancel button label (default "Cancel")
 */
export function confirmDelete({
  title,
  bodyHtml,
  confirmWord = 'DELETE',
  confirmLabel = 'Delete',
  cancelLabel = 'Cancel',
}) {
  return new Promise((resolve) => {
    const root = document.createElement('div');
    root.className = 'modal';
    root.innerHTML = `
      <div class="modal-content" role="dialog" aria-modal="true" aria-label="${title}">
        <h2>${title}</h2>
        <div style="color:var(--text-secondary);font-size:14px;line-height:1.5;margin-bottom:16px">${bodyHtml}</div>
        <label style="display:block;font-size:12px;color:var(--text-secondary);margin-bottom:6px">
          Type <code style="background:var(--bg-input,rgba(255,255,255,0.06));padding:1px 6px;border-radius:3px;font-family:var(--font-mono);color:var(--error)">${confirmWord}</code> to confirm
        </label>
        <input type="text"
               class="confirm-delete-input"
               autocomplete="off"
               spellcheck="false"
               style="width:100%;padding:8px 10px;font-family:var(--font-mono);font-size:13px;background:var(--bg-input,rgba(255,255,255,0.04));color:var(--text-primary);border:1px solid var(--border-mid);border-radius:4px;margin-bottom:18px">
        <div style="display:flex;justify-content:flex-end;gap:8px">
          <button type="button" class="btn btn-sm" data-action="cancel">${cancelLabel}</button>
          <button type="button" class="btn btn-sm" data-action="confirm" disabled
                  style="color:var(--error);border-color:var(--error);background:transparent">${confirmLabel}</button>
        </div>
      </div>
    `;

    const cleanup = (result) => {
      document.removeEventListener('keydown', onKey);
      root.remove();
      resolve(result);
    };

    const input = root.querySelector('.confirm-delete-input');
    const confirmBtn = root.querySelector('[data-action="confirm"]');
    const cancelBtn = root.querySelector('[data-action="cancel"]');

    input.addEventListener('input', () => {
      confirmBtn.disabled = input.value !== confirmWord;
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !confirmBtn.disabled) cleanup(true);
    });
    confirmBtn.addEventListener('click', () => cleanup(true));
    cancelBtn.addEventListener('click', () => cleanup(false));
    root.addEventListener('click', (e) => { if (e.target === root) cleanup(false); });

    const onKey = (e) => { if (e.key === 'Escape') cleanup(false); };
    document.addEventListener('keydown', onKey);

    document.body.appendChild(root);
    setTimeout(() => input.focus(), 0);
  });
}
