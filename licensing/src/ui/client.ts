/**
 * The one inline script for the portal and admin pages, pinned by CSP hash.
 * No inline handlers anywhere; everything is event delegation on data-
 * attributes, so no page needs a script of its own:
 *
 *   [data-action]       a button that sends its data-body as JSON
 *                       (data-method, default POST)
 *   form[data-json]     a form submitted as JSON to its action. Inputs with
 *                       data-num are numbers, data-list comma-separated lists,
 *                       data-date a YYYY-MM-DD date sent as epoch seconds (end
 *                       of that day, UTC); a file input with data-file-json and
 *                       a textarea with data-json-text are parsed as JSON.
 *                       data-template="/x/{seat_id}/y" fills the address from
 *                       a field instead, for a form that acts on a chosen seat.
 *   data-reveal         show named fields of the response ONCE, in a box with
 *                       a Copy button: a new seat key or token, which is never
 *                       shown again. The box is #reveal, or data-reveal-into.
 *   data-download       save the response's `file` as its `filename`
 *   data-autosave       on a form[data-json]: submitted as soon as one of its
 *                       fields changes (a row's model selects). A refused
 *                       change, or a cancelled confirm, puts the form back as
 *                       it was; the form is aria-busy while it is sent
 *   data-confirm        ask first; data-done the message on success (else
 *                       the response's note); data-reload reload after, the
 *                       message carried across it; data-next go there after
 *                       ({path.to.field} in it is filled from the response),
 *                       the message carried there too
 *   [data-toggle]       show or hide the element its value names (#id),
 *                       focusing its first field: a table's editor row
 *   [data-copy]         copy the value next to it
 *   [data-theme-toggle] cycle system / light / dark
 *
 * Every request is same-origin JSON, which is what the server's CSRF guard
 * expects. The light/dark choice is the only thing kept in localStorage; a
 * success message waits in sessionStorage across the reload it triggers.
 */
export const CLIENT_JS = `
(function () {
  'use strict';
  var flashTimer = null;
  var flash = function (text, kind) {
    var box = document.getElementById('flash');
    if (!box) { return; }
    box.textContent = text;
    box.className = kind === 'bad' ? 'flash bad' : 'flash';
    box.hidden = false;
    if (flashTimer) { clearTimeout(flashTimer); flashTimer = null; }
    if (kind !== 'bad') { flashTimer = setTimeout(function () { box.hidden = true; }, 6000); }
  };
  // A message that must outlive the reload that follows it.
  var CARRY = 'plexora-flash';
  try {
    var carried = sessionStorage.getItem(CARRY);
    if (carried) { sessionStorage.removeItem(CARRY); flash(carried, 'ok'); }
  } catch (e) { /* storage blocked: nothing carried */ }
  var LABELS = { key: 'Seat key', token: 'Licence token', id: 'Licence id' };
  var reveal = function (data, fields, selector) {
    var box = document.querySelector(selector || '#reveal'); if (!box) { return; }
    box.textContent = '';
    var secret = document.createElement('div'); secret.className = 'secret';
    var head = document.createElement('strong');
    head.textContent = 'Shown once. Copy it now: it cannot be shown again.';
    secret.appendChild(head);
    fields.split(',').forEach(function (path) {
      var value = path.split('.').reduce(function (o, k) { return o && o[k]; }, data);
      if (!value) { return; }
      var name = path.split('.').pop();
      var row = document.createElement('div'); row.className = 'secret-row';
      var label = document.createElement('span'); label.className = 'k'; label.textContent = LABELS[name] || name;
      var code = document.createElement('code'); code.textContent = String(value);
      var copy = document.createElement('button');
      copy.type = 'button'; copy.className = 'ghost tiny'; copy.textContent = 'Copy';
      copy.setAttribute('data-copy', '');
      row.appendChild(label); row.appendChild(code); row.appendChild(copy);
      secret.appendChild(row);
    });
    box.appendChild(secret);
    box.hidden = false;
    box.scrollIntoView({ block: 'nearest' });
  };
  var download = function (data) {
    if (!data || !data.file) { return false; }
    try {
      var blob = new Blob([data.file], { type: 'text/plain' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = data.filename || 'plexora.plexora';
      document.body.appendChild(a); a.click(); a.remove();
      return true;
    } catch (e) { return false; }
  };
  var send = function (url, method, body, el) {
    var button = el.tagName === 'BUTTON' ? el : el.querySelector('button[type=submit]');
    if (button) { button.disabled = true; }
    var autosave = el.tagName === 'FORM' && el.dataset.autosave !== undefined;
    if (el.tagName === 'FORM') { el.setAttribute('aria-busy', 'true'); }
    var done = function () { if (button) { button.disabled = false; } el.removeAttribute('aria-busy'); };
    return fetch(url, {
      method: method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: method === 'DELETE' ? undefined : body
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        done();
        if (!res.ok) {
          if (autosave) { el.reset(); }
          flash((data.error && data.error.message) || ('Failed (' + res.status + ').'), 'bad'); return;
        }
        if (el.dataset.reveal) { reveal(data, el.dataset.reveal, el.dataset.revealInto); }
        if (el.dataset.download !== undefined && !download(data)) {
          flash('Your browser blocked the download.', 'bad'); return;
        }
        if (el.dataset.next) {
          var carry = el.dataset.done || (typeof data.note === 'string' && data.note);
          if (carry) { try { sessionStorage.setItem(CARRY, carry); } catch (e) { /* the flash is lost */ } }
          window.location.href = el.dataset.next.replace(/\\{([\\w.]+)\\}/g, function (all, path) {
            var value = path.split('.').reduce(function (o, k) { return o == null ? o : o[k]; }, data);
            return encodeURIComponent(value == null ? '' : String(value));
          });
          return;
        }
        var message = el.dataset.done || (typeof data.note === 'string' && data.note)
          || (el.dataset.reveal ? 'Done. Copy it from the box below.' : 'Done.');
        flash(message, 'ok');
        if (el.dataset.reload !== undefined && !el.dataset.reveal) {
          try { sessionStorage.setItem(CARRY, message); } catch (e) { /* the flash is lost on reload */ }
          setTimeout(function () { window.location.reload(); }, 400);
        }
      });
    }, function () {
      done(); if (autosave) { el.reset(); }
      flash('Network error. Nothing was changed.', 'bad');
    });
  };
  var copyFrom = function (trigger) {
    var target = trigger.getAttribute('data-copy');
    var source = target ? document.querySelector(target)
      : trigger.closest('.secret-row, .keyline') && trigger.closest('.secret-row, .keyline').querySelector('code');
    var text = source ? (source.value || source.textContent || '') : '';
    if (!text || !navigator.clipboard || !navigator.clipboard.writeText) {
      flash('Select the text and copy it by hand.', 'bad'); return;
    }
    navigator.clipboard.writeText(text).then(
      function () { flash('Copied to the clipboard.', 'ok'); },
      function () { flash('Could not copy. Select the text and copy it by hand.', 'bad'); });
  };
  var THEMES = ['system', 'light', 'dark'];
  var storedTheme = function () {
    try { return localStorage.getItem('plexora-theme') || 'system'; } catch (e) { return 'system'; }
  };
  var applyTheme = function (value) {
    var root = document.documentElement;
    root.classList.remove('light', 'dark');
    if (value === 'light' || value === 'dark') { root.classList.add(value); }
    var icon = document.querySelector('[data-theme-icon]');
    if (icon) { icon.textContent = value === 'light' ? '\\u2600' : value === 'dark' ? '\\u263e' : '\\u25d0'; }
  };
  applyTheme(storedTheme());
  document.addEventListener('click', function (ev) {
    var target = ev.target;
    if (!target.closest) { return; }
    document.querySelectorAll('details.rowmenu[open]').forEach(function (menu) {
      if (!menu.contains(target)) { menu.removeAttribute('open'); }
    });
    var toggle = target.closest('[data-theme-toggle]');
    if (toggle) {
      var next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
      try { localStorage.setItem('plexora-theme', next); } catch (e) { /* private mode */ }
      applyTheme(next);
      return;
    }
    var toggler = target.closest('[data-toggle]');
    if (toggler) {
      ev.preventDefault();
      var panel = document.querySelector(toggler.getAttribute('data-toggle'));
      if (!panel) { return; }
      panel.hidden = !panel.hidden;
      toggler.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
      var first = panel.hidden ? null : panel.querySelector('select, input, textarea, button');
      if (first) { first.focus(); }
      return;
    }
    var copy = target.closest('[data-copy]');
    if (copy) { ev.preventDefault(); copyFrom(copy); return; }
    var el = target.closest('[data-action]');
    if (!el) { return; }
    ev.preventDefault();
    if (el.dataset.confirm && !window.confirm(el.dataset.confirm)) { return; }
    send(el.dataset.action, el.dataset.method || 'POST', el.dataset.body || '{}', el);
  });
  document.addEventListener('change', function (ev) {
    var form = ev.target && ev.target.form;
    if (!form || !form.matches('form[data-json]') || form.dataset.autosave === undefined) { return; }
    if (form.requestSubmit) { form.requestSubmit(); }
    else { form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true })); }
  });
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.matches || !form.matches('form[data-json]')) { return; }
    ev.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      if (form.dataset.autosave !== undefined) { form.reset(); }
      return;
    }
    var body = {}; var pending = []; var bad = null;
    Array.prototype.forEach.call(form.elements, function (input) {
      if (!input.name || input.disabled) { return; }
      if (input.type === 'checkbox') { body[input.name] = input.checked; }
      else if (input.type === 'file' && input.dataset.fileJson !== undefined) {
        var file = input.files && input.files[0]; if (!file) { return; }
        pending.push(file.text().then(function (text) {
          try { body[input.name] = JSON.parse(text); } catch (e) { body[input.name] = null; }
        }));
      }
      else if (input.dataset.jsonText !== undefined) {
        if (input.value.trim() === '') { return; }
        try { body[input.name] = JSON.parse(input.value); } catch (e) { bad = 'That is not valid JSON.'; }
      }
      else if (input.dataset.num !== undefined) { if (input.value !== '') { body[input.name] = Number(input.value); } }
      else if (input.dataset.date !== undefined) {
        if (input.value !== '') { body[input.name] = Math.floor(Date.parse(input.value + 'T23:59:59Z') / 1000); }
      }
      else if (input.dataset.list !== undefined) {
        body[input.name] = input.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      }
      else if (input.value !== '' || input.dataset.keepEmpty !== undefined) { body[input.name] = input.value; }
    });
    if (bad) { flash(bad, 'bad'); return; }
    Promise.all(pending).then(function () {
      var url = form.action;
      if (form.dataset.template) {
        url = form.dataset.template.replace(/\\{(\\w+)\\}/g, function (all, name) {
          var value = body[name]; delete body[name];
          return encodeURIComponent(value == null ? '' : String(value));
        });
      }
      send(url, form.dataset.method || 'POST', JSON.stringify(body), form);
    });
  });
})();
`;
