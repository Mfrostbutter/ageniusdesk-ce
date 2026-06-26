/**
 * Password policy checklist — a small live requirements list shared by the auth
 * gate (setup / reset) and Settings > Account (change password). Mirrors the
 * server policy returned in /api/auth/status.password_policy, so the rules shown
 * always match what the backend enforces.
 */

const DEFAULT_POLICY = {
  min_length: 12,
  require_upper: true,
  require_lower: true,
  require_number: true,
  require_symbol: true,
};

export function policyChecks(policy) {
  const p = policy || DEFAULT_POLICY;
  return [
    { id: 'len', label: `At least ${p.min_length} characters`, test: (pw) => pw.length >= p.min_length },
    p.require_upper && { id: 'upper', label: 'An uppercase letter (A-Z)', test: (pw) => /[A-Z]/.test(pw) },
    p.require_lower && { id: 'lower', label: 'A lowercase letter (a-z)', test: (pw) => /[a-z]/.test(pw) },
    p.require_number && { id: 'number', label: 'A number (0-9)', test: (pw) => /[0-9]/.test(pw) },
    p.require_symbol && { id: 'symbol', label: 'A symbol (!@#$…)', test: (pw) => /[^A-Za-z0-9]/.test(pw) },
  ].filter(Boolean);
}

/**
 * Render the checklist into `container` and keep it in sync with `input`.
 * Returns { isValid(): boolean, refresh() } — call isValid() before submit.
 */
export function mountChecklist(container, input, policy) {
  const checks = policyChecks(policy);
  container.classList.add('agd-pwck');
  container.innerHTML = checks.map(c =>
    `<div class="agd-pwck-item" data-id="${c.id}">
       <span class="agd-pwck-mark" aria-hidden="true"></span>
       <span class="agd-pwck-label">${c.label}</span>
     </div>`
  ).join('');

  const refresh = () => {
    const pw = input.value || '';
    let allOk = true;
    for (const c of checks) {
      const ok = c.test(pw);
      if (!ok) allOk = false;
      const row = container.querySelector(`[data-id="${c.id}"]`);
      if (row) row.classList.toggle('met', ok);
    }
    return allOk;
  };

  input.addEventListener('input', refresh);
  refresh();
  return { isValid: () => checks.every(c => c.test(input.value || '')), refresh };
}
