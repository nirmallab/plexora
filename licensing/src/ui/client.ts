/**
 * The one inline script for the portal and admin pages, pinned by CSP hash.
 * No inline handlers anywhere; it wires three things:
 *
 *   [data-action]       a button that POSTs its data-body as JSON
 *   form[data-json]     a form submitted as JSON (inputs with data-num are
 *                       numbers; a file input with data-file-json is read and
 *                       parsed, for the offline fingerprint report)
 *   data-reveal         show named fields of the response ONCE in #reveal --
 *                       a new seat key or token, which is never shown again
 *   data-download       save the response's `file` as its `filename`
 *
 * Every request is same-origin JSON, which is what the server's CSRF guard
 * expects. Nothing is kept in localStorage.
 */
export const CLIENT_JS = `
(function () {
  var flash = function (text) { var el = document.getElementById('flash'); if (el) el.textContent = text; };
  var reveal = function (data, fields) {
    var box = document.getElementById('reveal'); if (!box) return;
    box.textContent = '';
    var head = document.createElement('p');
    head.textContent = 'Shown once. Copy it now; it cannot be displayed again.';
    box.appendChild(head);
    fields.split(',').forEach(function (path) {
      var value = path.split('.').reduce(function (o, k) { return o && o[k]; }, data);
      if (!value) return;
      var code = document.createElement('code'); code.textContent = String(value);
      var row = document.createElement('p'); row.appendChild(code); box.appendChild(row);
    });
  };
  var download = function (data) {
    if (!data || !data.file) return;
    var blob = new Blob([data.file], { type: 'text/plain' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = data.filename || 'plexora.plexora';
    document.body.appendChild(a); a.click(); a.remove();
  };
  var send = function (url, method, body, el) {
    return fetch(url, {
      method: method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: method === 'DELETE' ? undefined : body
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) { flash((data.error && data.error.message) || ('Failed (' + res.status + ').')); return; }
        if (el.dataset.reveal) reveal(data, el.dataset.reveal);
        if (el.dataset.download !== undefined) download(data);
        if (el.dataset.next) { window.location.href = el.dataset.next; return; }
        flash(el.dataset.done || 'Done.');
        if (el.dataset.reload !== undefined && !el.dataset.reveal) window.location.reload();
      });
    }, function () { flash('Network error.'); });
  };
  document.addEventListener('click', function (ev) {
    var el = ev.target.closest && ev.target.closest('[data-action]');
    if (!el) return;
    ev.preventDefault();
    if (el.dataset.confirm && !window.confirm(el.dataset.confirm)) return;
    send(el.dataset.action, el.dataset.method || 'POST', el.dataset.body || '{}', el);
  });
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.matches || !form.matches('form[data-json]')) return;
    ev.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
    var body = {}; var pending = [];
    Array.prototype.forEach.call(form.elements, function (input) {
      if (!input.name) return;
      if (input.type === 'checkbox') body[input.name] = input.checked;
      else if (input.type === 'file' && input.dataset.fileJson !== undefined) {
        var file = input.files && input.files[0]; if (!file) return;
        pending.push(file.text().then(function (text) {
          try { body[input.name] = JSON.parse(text); } catch (e) { body[input.name] = null; }
        }));
      }
      else if (input.dataset.num !== undefined) { if (input.value !== '') body[input.name] = Number(input.value); }
      else if (input.dataset.list !== undefined) {
        body[input.name] = input.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      }
      else if (input.value !== '' || input.dataset.keepEmpty !== undefined) body[input.name] = input.value;
    });
    Promise.all(pending).then(function () {
      send(form.action, form.dataset.method || 'POST', JSON.stringify(body), form);
    });
  });
})();
`;
