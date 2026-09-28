/**
 * The one inline script, pinned by CSP hash. It only turns `data-action`
 * buttons and `form[data-json]` forms into same-origin JSON fetches, which is
 * what the admin API's same-origin guard expects. No inline handlers (CSP).
 */
export const CLIENT_JS = `
(function () {
  var flash = function (text) { var el = document.getElementById('flash'); if (el) el.textContent = text; };
  document.addEventListener('click', function (ev) {
    var el = ev.target.closest && ev.target.closest('[data-action]');
    if (!el) return;
    ev.preventDefault();
    if (el.dataset.confirm && !window.confirm(el.dataset.confirm)) return;
    var method = el.dataset.method || 'POST';
    fetch(el.dataset.action, {
      method: method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: method === 'DELETE' ? undefined : (el.dataset.body || '{}')
    }).then(function (res) {
      flash(res.ok ? 'Done.' : 'Failed (' + res.status + ').');
      if (res.ok && el.dataset.reload !== undefined) window.location.reload();
    }, function () { flash('Network error.'); });
  });
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.matches || !form.matches('form[data-json]')) return;
    ev.preventDefault();
    var body = {};
    Array.prototype.forEach.call(form.elements, function (input) {
      if (!input.name) return;
      if (input.type === 'checkbox') body[input.name] = input.checked;
      else if (input.dataset.num !== undefined) { if (input.value !== '') body[input.name] = Number(input.value); }
      else body[input.name] = input.value;
    });
    fetch(form.action, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (res) {
      if (res.ok && form.dataset.next) { window.location.href = form.dataset.next; return; }
      flash(res.ok ? 'Saved.' : 'Failed (' + res.status + ').');
      if (res.ok && form.dataset.reload !== undefined) window.location.reload();
    }, function () { flash('Network error.'); });
  });
})();
`;
