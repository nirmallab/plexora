/**
 * The one inline script, pinned by CSP hash. No inline handlers anywhere;
 * everything is event delegation on data- attributes (the same vocabulary as
 * the licence admin's script, trimmed to what these pages use):
 *
 *   [data-action]       a button that sends its data-body as JSON
 *                       (data-method, default POST)
 *   form[data-json]     a form submitted as JSON to its action; inputs with
 *                       data-num are sent as numbers
 *   data-confirm        ask first; data-done the message on success;
 *                       data-reload reload after; data-next go there after
 *   [data-theme-toggle] cycle system / light / dark
 *
 * Every request is same-origin JSON, which is what the admin API's
 * same-origin guard expects. The light/dark choice is the only thing kept in
 * localStorage.
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
    if (kind !== 'bad') { flashTimer = setTimeout(function () { box.hidden = true; }, 5000); }
  };
  var message = function (data, status) {
    var error = data && data.error;
    if (typeof error === 'string') { return error; }
    if (error && error.message) { return error.message; }
    return 'Failed (' + status + ').';
  };
  var send = function (url, method, body, el) {
    var button = el.tagName === 'BUTTON' ? el : el.querySelector('button[type=submit]');
    if (button) { button.disabled = true; }
    var done = function () { if (button) { button.disabled = false; } };
    return fetch(url, {
      method: method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: method === 'DELETE' ? undefined : body
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        done();
        if (!res.ok) { flash(message(data, res.status), 'bad'); return; }
        if (el.dataset.next) { window.location.href = el.dataset.next; return; }
        flash(el.dataset.done || 'Done.', 'ok');
        if (el.dataset.reload !== undefined) { setTimeout(function () { window.location.reload(); }, 400); }
      });
    }, function () { done(); flash('Network error. Nothing was changed.', 'bad'); });
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
    if (target.closest('[data-theme-toggle]')) {
      var next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
      try { localStorage.setItem('plexora-theme', next); } catch (e) { /* private mode */ }
      applyTheme(next);
      return;
    }
    var el = target.closest('[data-action]');
    if (!el) { return; }
    ev.preventDefault();
    if (el.dataset.confirm && !window.confirm(el.dataset.confirm)) { return; }
    send(el.dataset.action, el.dataset.method || 'POST', el.dataset.body || '{}', el);
  });
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.matches || !form.matches('form[data-json]')) { return; }
    ev.preventDefault();
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { return; }
    var body = {};
    Array.prototype.forEach.call(form.elements, function (input) {
      if (!input.name || input.disabled) { return; }
      if (input.type === 'checkbox') { body[input.name] = input.checked; }
      else if (input.dataset.num !== undefined) { if (input.value !== '') { body[input.name] = Number(input.value); } }
      else { body[input.name] = input.value; }
    });
    send(form.action, 'POST', JSON.stringify(body), form);
  });
})();
`;
