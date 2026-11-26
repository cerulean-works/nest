// Nest Management Console (G02a, redesigned in G04, with G02c onboarding).
//
// Runs on the Controller's auth listener and calls its JSON routes with the
// browser's HttpOnly session cookies; it never sees a token, password or key.
// Every decision is the server's: this script shows only the actions a role
// allows, and reports what the server answers when a request is refused anyway.
// Content is only ever inserted as text, never as HTML.
//
// Structure: DOM helpers and components, server calls, the app shell (Project
// switcher, navigation, account menu), then one view per route.
'use strict';

(function () {
  const PRIVILEGED_API = document.documentElement.dataset.privilegedApi === 'available';
  const ASSIGNABLE = ['admin', 'member', 'viewer'];
  const MANAGERS = ['owner', 'admin'];
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const LOGIN_URL = '/auth/google/login?next=console';
  const PAGE_SIZE = 15;
  // The browser remembers only which Project was last chosen here: its ID, never a
  // secret. It is checked against the server's list on every load.
  const STORE_PREFIX = 'nest.console.project.';

  const ERRORS = {
    not_found: 'This Project does not exist, or you no longer have access to it.',
    member_not_found: 'That person is not a member of this Project.',
    user_not_found: 'No active Nest user has that ID.',
    already_member: 'That user is already a member of this Project.',
    already_invited: 'There is already a pending invitation for that email address.',
    invitation_not_found: 'That invitation does not exist or is not addressed to you.',
    email_mismatch: 'This invitation was sent to a different email address.',
    invitation_unavailable: 'This invitation is no longer available.',
    owner_rule: 'The Owner’s membership cannot be changed this way.',
    invalid_request: 'A value is missing or not valid. Check the form and try again.',
    cross_origin: 'The request was refused because it did not come from this Console.',
    unavailable: 'Nest is unavailable. Try again later.',
    unauthenticated: 'Your session has ended. Sign in again.',
    platform_not_granted: 'Grant the Platform to this Project before linking an account on it.',
    account_not_found: 'That Slurm account is not registered on this Platform.',
    account_rule: 'That account link is not allowed.',
  };
  const REASONS = {
    role: 'Your role does not allow this.',
    owner_only: 'Only the Project Owner can do this.',
    owner_protected: 'The Owner’s membership cannot be changed or removed.',
    owner_by_transfer_only: 'The Owner role can be given only by an ownership transfer.',
    invalid_role: 'Choose Admin, Member or Viewer.',
    browser_only: 'This needs a browser session.',
    not_a_member: 'You are not a member.',
    platform_admin_required: 'Only platform administrators can do this.',
  };
  // Why a Project cannot run on this Controller yet (server-reported codes). No
  // reason names an account or another private identity.
  const INELIGIBLE = {
    controller: 'This Controller serves no HPC Platform yet, so no Project can run here. Contact the operator.',
    role: 'Your role cannot run work. Ask the Project’s Owner or an Admin for the Member role.',
    provider: 'The Project is not approved for this Controller’s Provider yet. A platform administrator approves it.',
    platform: 'This Controller’s HPC Platform is not granted to the Project. A platform administrator grants it.',
    account: 'The Project has no default Slurm account on this Platform. A platform administrator links one.',
  };

  const state = {
    session: null,
    admin: false,
    render: 0,
    flash: null,
    projects: null,
    connect: null,
    invitations: null,
    current: null,
    lost: null,
  };

  // DOM ---------------------------------------------------------------------

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === 'on') {
        for (const [event, fn] of Object.entries(value)) el.addEventListener(event, fn);
      } else if (name === 'text') {
        el.textContent = value;
      } else if (name === 'value' || name === 'checked') {
        el[name] = value;
      } else {
        el.setAttribute(name, value === true ? '' : String(value));
      }
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  let ids = 0;
  function uid(prefix) {
    ids += 1;
    return `${prefix}-${ids}`;
  }

  function field(label, input, hint) {
    const id = input.id || uid('f');
    input.id = id;
    const hintEl = hint ? h('span', { class: 'hint', id: `${id}-hint`, text: hint }) : null;
    if (hintEl) input.setAttribute('aria-describedby', hintEl.id);
    return h('div', { class: 'field' }, h('label', { for: id, text: label }), input, hintEl);
  }

  function checkbox(name, label, checked) {
    const id = uid('c');
    return h('div', { class: 'field check' }, h('input', { type: 'checkbox', name, id, checked }), h('label', { for: id, text: label }));
  }

  function roleSelect(name, selected) {
    return h(
      'select',
      { name, required: true },
      ASSIGNABLE.map((role) => h('option', { value: role, selected: role === selected, text: title(role) })),
    );
  }

  function title(role) {
    return role ? role.charAt(0).toUpperCase() + role.slice(1).replace('_', ' ') : '';
  }

  function when(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
  }

  function day(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString();
  }

  function short(id) {
    return String(id).slice(0, 8);
  }

  function badge(text, kind, attrs) {
    return h('span', { class: `badge ${kind || ''}`, ...(attrs || {}) }, text);
  }

  function roleBadge(role) {
    return badge(title(role), 'plain info', { title: 'Your role in this Project' });
  }

  function card(...children) {
    return h('section', { class: 'card' }, ...children);
  }

  function cardHead(text, id, extra) {
    return h('div', { class: 'card-head' }, h('h2', { id, text }), extra || null);
  }

  function callout(kind, ...children) {
    return h('div', { class: `callout ${kind || ''}` }, ...children);
  }

  function table(caption, headers, rows) {
    return h(
      'div',
      { class: 'table-wrap' },
      h(
        'table',
        { class: 'responsive' },
        h('caption', { class: 'sr-only', text: caption }),
        h('thead', {}, h('tr', {}, headers.map((t) => h('th', { scope: 'col' }, t ? t : h('span', { class: 'sr-only', text: 'Actions' }))))),
        h('tbody', {}, rows),
      ),
    );
  }

  // A table row whose cells carry their column name, for the stacked mobile layout.
  function row(headers, cells, attrs) {
    return h(
      'tr',
      attrs || {},
      cells.map((cell, i) => {
        const isActions = headers[i] === '';
        return h('td', { 'data-label': headers[i] || null, class: isActions ? 'actions' : null }, cell);
      }),
    );
  }

  function emptyState(heading, ...body) {
    return h('div', { class: 'empty' }, h('h2', { text: heading }), ...body);
  }

  function announce(message, kind) {
    const status = document.getElementById('status');
    status.textContent = '';
    if (message) status.append(h('p', { class: `notice ${kind || ''}`, text: message }));
  }

  function whisper(message) {
    // Screen-reader-only announcement, for feedback a sighted user sees in place.
    const el = document.getElementById('announcer');
    el.textContent = '';
    window.setTimeout(() => { el.textContent = message; }, 30);
  }

  function main() {
    return document.getElementById('main');
  }

  function show(...children) {
    const el = main();
    el.replaceChildren(...children.flat().filter(Boolean));
    const heading = el.querySelector('h1');
    if (heading) {
      heading.setAttribute('tabindex', '-1');
      heading.focus();
    }
  }

  function loading(heading) {
    return [
      h('h1', { text: heading }),
      h(
        'div',
        { class: 'card', 'aria-busy': 'true' },
        h('p', { class: 'sr-only', text: `Loading ${heading}…` }),
        h('span', { class: 'skeleton w60' }),
        h('span', { class: 'skeleton w80' }),
        h('span', { class: 'skeleton w40' }),
      ),
    ];
  }

  function errorBox(error, retry) {
    return h(
      'div',
      { class: 'error', role: 'alert' },
      h('p', { text: describe(error) }),
      retry ? h('button', { type: 'button', class: 'secondary', on: { click: retry }, text: 'Try again' }) : null,
    );
  }

  function crumbs(...items) {
    return h(
      'nav',
      { class: 'crumbs', 'aria-label': 'Breadcrumb' },
      h('ol', {}, items.map(([href, label]) => h('li', {}, href ? h('a', { href, text: label }) : h('span', { 'aria-current': 'page', text: label })))),
    );
  }

  function tabs(label, items, current) {
    return h(
      'nav',
      { class: 'tabs', 'aria-label': label },
      items.map(([key, href, text]) => h('a', { href, 'aria-current': key === current ? 'page' : null, text })),
    );
  }

  // Confirmation of a destructive change, saying what it does. Resolves to whether
  // the person confirmed; Escape and Cancel decline. Focus returns where it was.
  function confirmChange({ heading, body, action, danger = true }) {
    return new Promise((resolve) => {
      const opener = document.activeElement;
      const dialog = h('dialog', { class: 'confirm', 'aria-labelledby': 'confirm-h' });
      const done = (answer) => {
        dialog.close();
        dialog.remove();
        if (opener && typeof opener.focus === 'function' && document.contains(opener)) opener.focus();
        resolve(answer);
      };
      const cancel = h('button', { type: 'button', class: 'secondary', text: 'Cancel', on: { click: () => done(false) } });
      dialog.append(
        h('h2', { id: 'confirm-h', text: heading }),
        ...(Array.isArray(body) ? body : [body]).map((text) => h('p', { text })),
        h(
          'div',
          { class: 'actions-row' },
          cancel,
          h('button', { type: 'button', class: danger ? 'danger-solid' : '', text: action, on: { click: () => done(true) } }),
        ),
      );
      dialog.addEventListener('cancel', (event) => {
        event.preventDefault();
        done(false);
      });
      document.body.append(dialog);
      dialog.showModal();
      cancel.focus();
    });
  }

  // Server calls ---------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, body) {
      super((body && body.error) || `HTTP ${status}`);
      this.status = status;
      this.code = (body && body.error) || null;
      this.reason = (body && body.reason) || null;
    }
  }

  function describe(error) {
    if (!(error instanceof ApiError)) return 'Nest could not be reached. Check your connection and try again.';
    if (error.code === 'forbidden') return REASONS[error.reason] || 'You are not allowed to do this.';
    if (error.code && ERRORS[error.code]) return ERRORS[error.code];
    if (error.status === 404) return 'Not found.';
    if (error.status >= 500) return 'Nest could not complete the request. Try again later.';
    return 'The request was refused.';
  }

  function send(method, path, body) {
    const init = {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      headers: { Accept: 'application/json' },
    };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    return fetch(path, init);
  }

  async function parse(response) {
    try {
      return await response.json();
    } catch (_) {
      return {};
    }
  }

  let refreshing = null;
  function refresh() {
    // One refresh at a time: the refresh secret rotates, so a second concurrent
    // use of the old one would be treated as a replay and end the session.
    if (!refreshing) {
      refreshing = send('POST', '/auth/session/refresh')
        .then(async (r) => {
          if (!r.ok) return false;
          state.session = { ...(state.session || {}), ...(await parse(r)) };
          return true;
        })
        .catch(() => false)
        .finally(() => {
          refreshing = null;
        });
    }
    return refreshing;
  }

  async function api(method, path, body) {
    // Every API change is a JSON POST: the server refuses any other content type,
    // which also keeps a cross-site form from posting without a CORS preflight.
    if (method === 'POST' && body === undefined) body = {};
    let response = await send(method, path, body);
    if (response.status === 401 && (await refresh())) response = await send(method, path, body);
    const data = await parse(response);
    if (response.status === 401) {
      signedOut(ERRORS.unauthenticated);
      throw new ApiError(401, data);
    }
    if (!response.ok) throw new ApiError(response.status, data);
    return data;
  }

  async function loadSession() {
    let response = await send('GET', '/auth/session');
    if (response.status === 401 && (await refresh())) response = await send('GET', '/auth/session');
    if (response.status === 401) return null;
    if (!response.ok) throw new ApiError(response.status, await parse(response));
    return parse(response);
  }

  async function loadAdmin() {
    // The server's answer, not a guess: 200 for an active platform admin, 403 otherwise.
    if (!PRIVILEGED_API) return false;
    try {
      await api('GET', '/api/admin/users?limit=1');
      return true;
    } catch (_) {
      return false;
    }
  }

  // The signed-in User's Projects, their compute eligibility and pending
  // invitations: read again on every page, so the shell never shows stale access.
  async function loadContext() {
    const [projects, connect, invitations] = await Promise.all([
      api('GET', '/api/projects'),
      api('GET', '/api/connect').catch((error) => {
        if (error instanceof ApiError && error.status === 401) throw error;
        return null;
      }),
      api('GET', '/api/invitations').catch((error) => {
        if (error instanceof ApiError && error.status === 401) throw error;
        return null;
      }),
    ]);
    state.projects = projects.projects;
    state.connect = connect;
    state.invitations = invitations ? invitations.invitations : null;
    chooseCurrent();
  }

  // Projects, names and the current Project -----------------------------------

  function storeKey() {
    return state.session ? STORE_PREFIX + state.session.user_id : null;
  }

  function remembered() {
    try {
      const value = window.localStorage.getItem(storeKey());
      return value && UUID_RE.test(value) ? value.toLowerCase() : null;
    } catch (_) {
      return null;
    }
  }

  function remember(id) {
    try {
      if (id) window.localStorage.setItem(storeKey(), id);
      else window.localStorage.removeItem(storeKey());
    } catch (_) {
      // Private browsing: the choice lasts for this page only.
    }
  }

  function chooseCurrent() {
    const list = state.projects || [];
    const wanted = state.current || remembered();
    if (wanted && list.some((p) => p.id === wanted)) {
      state.current = wanted;
      return;
    }
    if (wanted) state.lost = wanted;
    const ready = list.find((p) => eligibility(p.id).eligible);
    state.current = (ready || list[0] || {}).id || null;
    remember(state.current);
  }

  function setCurrent(id) {
    state.current = id;
    state.lost = null;
    remember(id);
  }

  function currentProject() {
    return (state.projects || []).find((p) => p.id === state.current) || null;
  }

  function eligibility(id) {
    const report = state.connect && state.connect.eligibility;
    if (!report || !report.evaluated) return { known: false, eligible: false, reasons: [] };
    const found = report.projects.find((p) => p.id === id);
    if (!found) return { known: true, eligible: false, reasons: ['controller'] };
    return { known: true, eligible: !!found.eligible, reasons: found.reasons || [] };
  }

  // A label that tells Projects apart: the name, and the start of the ID when
  // several of the caller's Projects share that name.
  function labelOf(project, list) {
    const all = list || state.projects || [];
    const twins = all.filter((p) => p.name.toLowerCase() === project.name.toLowerCase());
    if (twins.length < 2) return project.name;
    let size = 8;
    while (size < 36 && twins.some((p) => p.id !== project.id && p.id.slice(0, size) === project.id.slice(0, size))) size += 1;
    return `${project.name} (${project.id.slice(0, size)})`;
  }

  function computeBadge(id) {
    const e = eligibility(id);
    if (!e.known) return badge('Eligibility not reported', 'plain', { title: 'This Nest does not report where Projects may run' });
    return e.eligible ? badge('Can run here', 'ok') : badge('Cannot run yet', 'warn');
  }

  function reasonsList(id) {
    const e = eligibility(id);
    if (!e.known || e.eligible) return null;
    return h('ul', {}, e.reasons.map((r) => h('li', { text: INELIGIBLE[r] || 'A requirement is missing.' })));
  }

  // Shell -----------------------------------------------------------------------

  function initials(session) {
    const source = (session.display_name || session.email || '?').trim();
    const parts = source.split(/\s+/).filter(Boolean);
    return ((parts[0] || '?')[0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
  }

  function renderShell() {
    const nav = document.getElementById('nav');
    const account = document.getElementById('account');
    const switcher = document.getElementById('switcher');
    const shell = document.getElementById('shell');
    if (!state.session) {
      nav.replaceChildren();
      account.replaceChildren();
      switcher.replaceChildren();
      shell.classList.add('signed-out');
      return;
    }
    shell.classList.remove('signed-out');
    const section = location.hash.replace(/^#\/?/, '').split('/')[0] || 'overview';
    const pending = state.invitations ? state.invitations.length : 0;
    const link = (key, href, label, count) =>
      h('li', {}, h('a', { href, 'aria-current': section === key ? 'page' : null }, h('span', { text: label }), count ? h('span', { class: 'count', 'aria-label': `${count} pending`, text: String(count) }) : null));
    const items = [
      link('overview', '#/overview', 'Overview'),
      link('connect', '#/connect', 'Connect'),
      link('projects', '#/projects', 'Projects'),
      link('invitations', '#/invitations', 'Invitations', pending),
    ];
    const admin = state.admin
      ? [h('li', { class: 'nav-group', 'aria-hidden': 'true', text: 'Platform' }), link('admin', '#/admin', 'Administration')]
      : [];
    nav.replaceChildren(h('ul', {}, items, admin));

    const list = state.projects || [];
    if (list.length) {
      const select = h(
        'select',
        { id: 'project-switcher', on: { change: () => switchTo(select.value) } },
        list.map((p) => h('option', { value: p.id, selected: p.id === state.current, text: `${labelOf(p)} · ${title(p.role)}` })),
      );
      switcher.replaceChildren(h('label', { for: 'project-switcher', text: 'Current Project' }), select);
    } else {
      switcher.replaceChildren();
    }

    const who = state.session;
    const name = who.display_name || who.email || 'Signed in';
    account.replaceChildren(
      h(
        'details',
        { class: 'account' },
        h('summary', { 'aria-label': `Account: ${name}` }, h('span', { class: 'avatar', 'aria-hidden': 'true', text: initials(who) }), h('span', { class: 'account-name', text: name })),
        h(
          'div',
          { class: 'account-menu' },
          h('p', {}, h('strong', { text: who.display_name || 'Signed in' })),
          who.email ? h('p', { class: 'muted small', text: who.email }) : null,
          h('p', { class: 'small muted' }, 'User ID ', h('span', { class: 'mono', text: who.user_id })),
          state.admin ? h('p', {}, badge('Platform administrator', 'info')) : null,
          h('button', { type: 'button', class: 'secondary', on: { click: signOut }, text: 'Sign out' }),
        ),
      ),
    );
  }

  function switchTo(id) {
    if (!UUID_RE.test(id)) return;
    setCurrent(id);
    const project = currentProject();
    whisper(project ? `Current Project: ${labelOf(project)}` : 'Current Project changed');
    const parts = location.hash.replace(/^#\/?/, '').split('/');
    if (parts[0] === 'projects' && parts.length >= 2) {
      location.hash = `#/projects/${id}${parts[2] ? `/${parts[2]}` : ''}`;
    } else {
      route();
    }
  }

  function signedOut(message) {
    state.session = null;
    state.admin = false;
    state.projects = null;
    state.current = null;
    renderShell();
    announce('');
    show(
      h(
        'div',
        { class: 'signin' },
        h('div', { class: 'card' },
          h('h1', { text: 'Sign in to Nest' }),
          message ? h('p', { class: 'error', role: 'alert', text: message }) : null,
          h('p', { class: 'lead', text: 'Manage your Nest Projects, members and invitations, and connect your computer to run work.' }),
          h('a', { class: 'btn', href: LOGIN_URL, text: 'Sign in with Google' }),
          h('p', { class: 'small muted', text: 'Nest never asks for your HPC password or one-time code.' }),
        ),
      ),
    );
  }

  async function signOut() {
    try {
      await send('POST', '/auth/session/logout');
    } catch (_) {
      // The cookies are cleared by the server; without it, signing out again works.
    }
    history.replaceState(null, '', location.pathname);
    signedOut('You have signed out.');
  }

  // Routing -----------------------------------------------------------------------

  async function route() {
    const token = ++state.render;
    const current = () => token === state.render;
    if (!state.session) return signedOut();
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    try {
      await loadContext();
      if (!current()) return;
      renderShell();
      const [section, id, tab] = parts;
      if (!section || section === 'overview') return overviewView();
      if (section === 'connect' && parts.length === 1) return connectView();
      if (section === 'projects' && parts.length === 1) return projectsView();
      if (section === 'projects' && id && UUID_RE.test(id) && parts.length <= 3) {
        return await projectView(id.toLowerCase(), tab || 'members', current);
      }
      if (section === 'invitations' && parts.length === 1) return invitationsView();
      if (section === 'admin' && !state.admin) return adminUnavailable();
      if (section === 'admin') {
        if (parts.length === 1 || (parts.length === 2 && ['projects', 'new', 'users'].includes(id))) {
          return await adminView(id || 'projects', current);
        }
        if (id === 'projects' && parts.length === 3 && UUID_RE.test(tab)) return await accessView(tab, current);
      }
      show(h('h1', { text: 'Page not found' }), h('p', {}, h('a', { href: '#/overview', text: 'Back to the overview' })));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return;
      if (!current()) return;
      if (adminRevoked(error)) return;
      renderShell();
      show(h('h1', { text: 'Something went wrong' }), errorBox(error, route));
    }
  }

  // When the server no longer counts the caller as a platform admin, drops the
  // Administration pages wherever the refusal arrives. Returns whether it did.
  function adminRevoked(error) {
    if (!(error instanceof ApiError && error.reason === 'platform_admin_required')) return false;
    state.admin = false;
    renderShell();
    adminUnavailable();
    return true;
  }

  function adminUnavailable() {
    show(
      h('h1', { text: 'Administration not available' }),
      h('p', { class: 'error', role: 'alert', text: REASONS.platform_admin_required }),
      h('p', {}, h('a', { href: '#/overview', text: 'Back to the overview' })),
    );
  }

  // A change: runs it, reports the outcome and re-reads the page, since what the
  // caller may do can have changed (a role change, a removal, a revoked session).
  // A recoverable failure keeps the form as it was, with focus on the message.
  async function change(form, run, done) {
    const error = form ? form.querySelector('.form-error') : null;
    const buttons = form ? form.querySelectorAll('button') : [];
    if (error) error.textContent = '';
    buttons.forEach((b) => {
      b.disabled = true;
      b.setAttribute('aria-busy', 'true');
    });
    try {
      const result = await run();
      // Re-read first, so the message describes the page as it now is.
      await route();
      if (done) announce(typeof done === 'function' ? done(result) : done, 'ok');
      return result;
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) return null;
      const message = describe(failure);
      if (error) {
        error.textContent = message;
        error.setAttribute('tabindex', '-1');
      }
      announce(message, 'bad');
      if (adminRevoked(failure)) return null;
      if (failure instanceof ApiError && [403, 404, 410].includes(failure.status)) await route();
      else if (error) error.focus();
      return null;
    } finally {
      buttons.forEach((b) => {
        b.disabled = false;
        b.removeAttribute('aria-busy');
      });
    }
  }

  function formError() {
    return h('p', { class: 'form-error', role: 'alert' });
  }

  function values(form) {
    return Object.fromEntries(new FormData(form).entries());
  }

  // Copyable commands ---------------------------------------------------------------

  async function copyText(text, button, pre) {
    let copied = false;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        copied = true;
      }
    } catch (_) {
      copied = false;
    }
    if (!copied) {
      // No clipboard access: select the command so the person can copy it.
      const range = document.createRange();
      range.selectNodeContents(pre);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      pre.focus();
    }
    const label = copied ? 'Copied' : 'Selected: press Ctrl+C';
    button.textContent = label;
    whisper(copied ? 'Copied to the clipboard.' : 'The command is selected; press Control C or Command C to copy it.');
    window.setTimeout(() => {
      if (button.isConnected) button.textContent = 'Copy';
    }, 2500);
  }

  function codeBlock(label, lines) {
    const text = lines.join('\n');
    const pre = h('pre', { tabindex: '0', 'aria-label': label }, h('code', { text }));
    const button = h('button', { type: 'button', class: 'copy', 'aria-label': `Copy: ${label}`, text: 'Copy' });
    button.addEventListener('click', () => copyText(text, button, pre));
    return h('div', { class: 'code', 'data-command': label }, pre, button);
  }

  // Overview ------------------------------------------------------------------------

  function overviewView() {
    const list = state.projects;
    const who = state.session;
    const project = currentProject();
    const pending = state.invitations ? state.invitations.length : 0;
    const ready = list.filter((p) => eligibility(p.id).eligible).length;
    const known = !!(state.connect && state.connect.eligibility && state.connect.eligibility.evaluated);
    const greeting = who.display_name ? `Welcome, ${who.display_name}` : 'Welcome to Nest';

    const stats = h(
      'div',
      { class: 'stats' },
      h('div', { class: 'stat' }, h('span', { class: 'stat-value', text: String(list.length) }), h('span', { class: 'stat-label', text: list.length === 1 ? 'Project' : 'Projects' })),
      known ? h('div', { class: 'stat' }, h('span', { class: 'stat-value', text: String(ready) }), h('span', { class: 'stat-label', text: 'can run on this Controller' })) : null,
      h('div', { class: 'stat' }, h('span', { class: 'stat-value', text: state.invitations ? String(pending) : '—' }), h('span', { class: 'stat-label', text: 'pending invitations' })),
    );

    const lost = state.lost
      ? callout('warn', h('p', { text: 'The Project you last used here is no longer available to you, so another one is selected.' }))
      : null;

    let focus;
    if (!list.length) {
      focus = emptyState(
        'You are not in any Project yet',
        h('p', { class: 'muted', text: 'A Project gives you access to run work. Ask a Project Owner or Admin to invite you, then accept the invitation here.' }),
        h('div', { class: 'actions-row' }, h('a', { class: 'btn', href: '#/invitations', text: pending ? `Review ${pending} invitation${pending === 1 ? '' : 's'}` : 'Join with an invitation link' })),
      );
    } else if (project) {
      const e = eligibility(project.id);
      focus = card(
        cardHead('Current Project', 'current-h', h('div', { class: 'badges' }, roleBadge(project.role), ' ', computeBadge(project.id))),
        h('p', { class: 'cell-main', text: labelOf(project) }),
        e.known && !e.eligible
          ? callout('warn', h('p', {}, h('strong', { text: 'This Project cannot run work on this Controller yet.' })), reasonsList(project.id))
          : e.eligible
            ? h('p', { class: 'muted', text: 'Everything needed to run work is in place: your role, the Provider approval, the Platform grant and a default Slurm account.' })
            : h('p', { class: 'muted', text: 'This Nest does not report which Projects may run here; the Controller decides on every call.' }),
        h(
          'div',
          { class: 'actions-row' },
          h('a', { class: 'btn', href: '#/connect', text: 'Connect from your computer' }),
          h('a', { class: 'btn secondary', href: `#/projects/${project.id}`, text: MANAGERS.includes(project.role) ? 'Manage Project' : 'Open Project' }),
        ),
      );
    }

    const steps = [];
    if (list.length) steps.push(['Connect your computer', 'Install Nest, sign in from the command line and select your Project.', '#/connect', 'Open the setup steps']);
    if (pending) steps.push([`${pending} pending invitation${pending === 1 ? '' : 's'}`, 'Review and accept the Projects you were invited to.', '#/invitations', 'Review invitations']);
    if (project && MANAGERS.includes(project.role)) steps.push(['Invite your team', `Invite people to ${labelOf(project)} by email or with an Invite Link.`, `#/projects/${project.id}/invitations`, 'Invite people']);
    if (state.admin) steps.push(['Platform administration', 'Create Projects, grant Platforms and link Slurm accounts.', '#/admin', 'Open Administration']);
    if (!list.length) steps.push(['Need a new Project?', 'Projects are created by a platform administrator, who names their Owner. Send them your User ID from the account menu.', null, null]);

    show(
      h('div', { class: 'page-head' }, h('div', {}, h('h1', { text: 'Overview' }), h('p', { class: 'lead', text: greeting }))),
      lost,
      stats,
      h(
        'div',
        { class: 'grid' },
        focus,
        card(
          cardHead('Next steps', 'next-h'),
          h('ul', { class: 'next-steps' }, steps.map(([head, text, href, action]) =>
            h('li', {}, h('span', { class: 'dot', 'aria-hidden': 'true' }), h('div', {}, h('strong', { text: head }), h('p', { class: 'muted small', text }), href ? h('a', { href, text: action }) : null)),
          )),
        ),
      ),
      list.length
        ? card(
            cardHead('Your Projects', 'yours-h', h('a', { href: '#/projects', text: `All Projects (${list.length})` })),
            projectRows(list.slice(0, 5), 'Your Projects'),
          )
        : null,
    );
  }

  // Connect to Project (G02c) ---------------------------------------------------------

  function connectView() {
    const info = state.connect;
    const list = state.projects;
    show(
      h('div', { class: 'page-head' }, h('div', {}, h('h1', { text: 'Connect to a Project' }), h('p', { class: 'lead', text: 'Set up your computer once, then connect and choose the Project your remote functions run in.' }))),
    );
    if (!info) {
      main().append(callout('bad', h('p', { text: 'Nest could not load the connection settings. Try again.' }), h('button', { type: 'button', class: 'secondary', text: 'Try again', on: { click: route } })));
      return;
    }
    const profiles = info.profiles || [];
    let profile = profiles[0] || null;
    const picker = list.length
      ? h(
          'select',
          { id: 'connect-project', on: { change: () => { setCurrent(picker.value); renderShell(); renderSteps(); } } },
          list.map((p) => {
            const e = eligibility(p.id);
            const suffix = e.known ? (e.eligible ? 'can run here' : 'cannot run yet') : title(p.role);
            return h('option', { value: p.id, selected: p.id === state.current, text: `${labelOf(p)} · ${suffix}` });
          }),
        )
      : null;
    const profileChoice = profiles.length > 1
      ? h(
          'fieldset',
          {},
          h('legend', { text: 'How do you connect?' }),
          h('div', { class: 'choices' }, profiles.map((p, i) => {
            const id = uid('profile');
            const input = h('input', { type: 'radio', name: 'profile', id, value: p.id, checked: i === 0, on: { change: () => { profile = p; renderSteps(); } } });
            return h('label', { class: 'choice', for: id }, input, h('span', {}, h('strong', {}, p.label, ' ', p.status === 'pilot' ? badge('Pilot', 'warn') : null), h('span', { class: 'small muted', text: p.summary })));
          })),
        )
      : null;
    const steps = h('div', { id: 'connect-steps' });
    const setup = card(
      cardHead('Your Project', 'connect-project-h'),
      picker
        ? field('Project', picker, 'Projects that cannot run on this Controller yet say what is missing.')
        : callout('', h('p', {}, 'You are not in any Project yet. You can still install Nest and sign in; ', h('a', { href: '#/invitations', text: 'accept an invitation' }), ' to run work.')),
      h('div', { id: 'connect-status' }),
      profiles.length === 1
        ? h('p', { class: 'small muted' }, h('strong', {}, `${profiles[0].label}: `, profiles[0].status === 'pilot' ? badge('Pilot', 'warn') : null), ' ', profiles[0].summary)
        : null,
      profileChoice,
      profiles.length
        ? null
        : callout('warn', h('p', { text: 'The operator has not published connection settings for this Nest in the Console yet. Ask them for the Controller address, then follow the steps below.' })),
    );
    main().append(setup, steps);

    function renderSteps() {
      const project = currentProject();
      const status = document.getElementById('connect-status');
      const e = project ? eligibility(project.id) : null;
      status.replaceChildren(
        project && e.known && !e.eligible
          ? callout('warn', h('p', {}, h('strong', { text: `${labelOf(project)} cannot run on this Controller yet.` })), reasonsList(project.id))
          : project && e.eligible
            ? callout('ok', h('p', { text: `${labelOf(project)} can run on this Controller.` }))
            : null,
      );
      const exports = profile ? profile.exports : [info.auth_export];
      const pid = project && UUID_RE.test(project.id) ? project.id.toLowerCase() : null;
      const several = list.filter((p) => eligibility(p.id).eligible).length > 1 || !(e && e.known);
      const items = [
        h('li', {},
          h('h3', { text: 'Install Nest (once per uv project)' }),
          h('p', { class: 'muted small', text: 'In your uv project directory. Nest needs Python 3.14 or later.' }),
          codeBlock('Install Nest', [info.install]),
        ),
        h('li', {},
          h('h3', { text: profile ? 'Connect and sign in (each new terminal)' : 'Sign in (each new terminal)' }),
          h('p', { class: 'muted small', text: !profile
            ? 'This names the Nest you sign in to. Sign-in opens a browser approval page; no password is typed in the terminal.'
            : profile.transport === 'private-network'
              ? 'These settings name the Controller on your private network. Connect your device to that network first.'
              : 'These settings name the Controller and the Nest you sign in to. Sign-in opens a browser approval page; no password is typed in the terminal.' }),
          codeBlock('Connection settings and sign-in', [...exports, ...info.setup.map((s) => s.command)]),
        ),
      ];
      if (!profile) {
        // Without the Controller's settings, commands that contact it would fail.
        items.push(
          h('li', { id: 'connect-needs-controller' },
            h('h3', { text: 'Select the Project and run' }),
            callout('warn', h('p', { text: 'Selecting a Project and running functions need the Controller’s connection settings, which the operator has not published here. Ask them for the Controller address and transport, then use: uv run nest projects select' })),
          ),
        );
        steps.replaceChildren(card(cardHead('Set up your computer', 'steps-h'), h('ol', { class: 'steps' }, items)));
        return;
      }
      if (pid && e && (e.eligible || !e.known)) {
        items.push(
          h('li', {},
            h('h3', { text: 'Select the Project' }),
            h('p', { class: 'muted small', text: several
              ? 'Saves this Project for this Controller and account. To pick from a list instead, run: uv run nest projects select'
              : 'Optional: with exactly one Project that can run here, the SDK uses it automatically. Saving it keeps that choice if you join more Projects.' }),
            codeBlock('Select the Project', [`uv run nest projects use ${pid}`, 'uv run nest projects current']),
            h('p', { class: 'muted small' }, 'An exported NEST_PROJECT_ID always takes precedence over the saved choice.'),
          ),
        );
      }
      items.push(
        h('li', {},
          h('h3', { text: 'Run a remote function' }),
          h('p', { class: 'muted small', text: 'Save this as main.py, then run it. The Controller checks your access again on every call.' }),
          codeBlock('Example main.py', [
            'import nest',
            '',
            '',
            '@nest.remote()',
            'def add(a, b):',
            '    return a + b',
            '',
            '',
            'print(add.remote(40, 2))  # 42',
          ]),
          codeBlock('Run the example', ['uv run python main.py']),
        ),
      );
      steps.replaceChildren(card(cardHead('Set up your computer', 'steps-h'), h('ol', { class: 'steps' }, items)));
    }
    renderSteps();
  }

  // Projects ------------------------------------------------------------------------

  function projectRows(list, caption) {
    const headers = ['Project', 'Your role', 'On this Controller', 'Created', ''];
    return table(
      caption,
      headers,
      list.map((p) =>
        row(headers, [
          h('span', {}, h('a', { class: 'cell-main', href: `#/projects/${p.id}`, text: labelOf(p) })),
          title(p.role),
          computeBadge(p.id),
          day(p.created_at),
          p.id === state.current
            ? badge('Current', 'plain info')
            : h('button', { type: 'button', class: 'secondary sm', 'aria-label': `Make ${labelOf(p)} the current Project`, text: 'Make current', on: { click: () => switchTo(p.id) } }),
        ]),
      ),
    );
  }

  function projectsView() {
    const list = state.projects;
    if (!list.length) {
      show(
        h('h1', { text: 'Your Projects' }),
        emptyState(
          'You are not a member of any Project yet.',
          h('p', {}, 'Ask a Project Owner or Admin to invite you, then accept the invitation under ', h('a', { href: '#/invitations', text: 'Invitations' }), '.'),
          h('p', { class: 'muted small', text: 'New Projects are created by a platform administrator, who names their Owner.' }),
        ),
      );
      return;
    }
    const search = h('input', { type: 'search', id: 'project-search', autocomplete: 'off', placeholder: 'Name' });
    const roleFilter = h('select', { id: 'project-role' }, h('option', { value: '', text: 'All roles' }), ['owner', ...ASSIGNABLE].map((r) => h('option', { value: r, text: title(r) })));
    const results = h('div', { id: 'project-results' });
    let page = 0;
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const matching = list.filter((p) => (!q || p.name.toLowerCase().includes(q) || p.id.startsWith(q)) && (!roleFilter.value || p.role === roleFilter.value));
      const pages = Math.max(1, Math.ceil(matching.length / PAGE_SIZE));
      page = Math.min(page, pages - 1);
      const slice = matching.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
      results.replaceChildren(
        matching.length ? projectRows(slice, 'Projects you are a member of') : h('p', { class: 'empty', text: 'No Project matches.' }),
        pager(matching.length, page, pages, (next) => { page = next; draw(); }),
      );
    };
    search.addEventListener('input', () => { page = 0; draw(); });
    roleFilter.addEventListener('change', () => { page = 0; draw(); });
    show(
      h('div', { class: 'page-head' }, h('div', {}, h('h1', { text: 'Your Projects' }), h('p', { class: 'lead', text: 'Your role in each Project decides what you can manage; whether it can run work here depends on approvals the operator grants.' }))),
      h('div', { class: 'toolbar', role: 'search' }, field('Search Projects', search), h('div', { class: 'field narrow' }, h('label', { for: 'project-role', text: 'Role' }), roleFilter)),
      results,
      h('p', { class: 'muted small', text: 'New Projects are created by a platform administrator, who names their Owner.' }),
    );
    draw();
  }

  function pager(total, page, pages, go) {
    if (total <= PAGE_SIZE) return h('p', { class: 'pager', text: `${total} shown` });
    const first = page * PAGE_SIZE + 1;
    const last = Math.min(total, (page + 1) * PAGE_SIZE);
    return h(
      'div',
      { class: 'pager' },
      h('span', { 'aria-live': 'polite', text: `${first}–${last} of ${total}` }),
      h(
        'span',
        { class: 'row-inline' },
        h('button', { type: 'button', class: 'secondary sm', disabled: page === 0, text: 'Previous', on: { click: () => go(page - 1) } }),
        h('button', { type: 'button', class: 'secondary sm', disabled: page >= pages - 1, text: 'Next', on: { click: () => go(page + 1) } }),
      ),
    );
  }

  function projectUnavailable() {
    show(
      h('h1', { text: 'Project not available' }),
      h('p', { class: 'error', role: 'alert', text: ERRORS.not_found }),
      h('p', {}, h('a', { href: '#/projects', text: 'Back to your Projects' })),
    );
  }

  async function projectView(pid, tab, current) {
    show(...loading('Project'));
    let project, members;
    try {
      [project, { members }] = await Promise.all([
        api('GET', `/api/projects/${pid}`),
        api('GET', `/api/projects/${pid}/members`),
      ]);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        if (current()) projectUnavailable();
        return;
      }
      throw error;
    }
    if (!current()) return;
    if (state.current !== pid) {
      setCurrent(pid);
      renderShell();
    }
    const role = project.role;
    const manager = MANAGERS.includes(role);
    if (!manager && tab !== 'members') tab = 'members';
    const names = new Map(members.map((m) => [m.user_id, m.display_name]));
    const owner = members.find((m) => m.role === 'owner');
    const base = `#/projects/${pid}`;
    const head = [
      crumbs(['#/projects', 'Projects'], [null, labelOf(project)]),
      h(
        'div',
        { class: 'page-head' },
        h('div', {}, h('h1', { text: project.name }), h('div', { class: 'badges' }, roleBadge(role), computeBadge(pid))),
        h('a', { class: 'btn secondary', href: '#/connect', text: 'Connect to this Project' }),
      ),
      card(
        h(
          'dl',
          { class: 'facts' },
          h('dt', { text: 'Owner' }), h('dd', { text: owner ? owner.display_name || 'Unnamed user' : '—' }),
          h('dt', { text: 'Your role' }), h('dd', { text: title(role) }),
          h('dt', { text: 'Created' }), h('dd', { text: when(project.created_at) }),
          h('dt', { text: 'Project ID' }), h('dd', {}, h('span', { class: 'mono', text: project.id })),
        ),
        eligibility(pid).known && !eligibility(pid).eligible
          ? callout('warn', h('p', {}, h('strong', { text: 'Running work here still needs:' })), reasonsList(pid))
          : null,
      ),
      manager
        ? tabs('Project sections', [['members', base, `Members (${members.length})`], ['invitations', `${base}/invitations`, 'Invitations'], ['activity', `${base}/activity`, 'Activity']], tab)
        : null,
    ];
    if (tab === 'invitations') {
      const [{ invitations }, { invite_links: links }] = await Promise.all([
        api('GET', `/api/projects/${pid}/invitations`),
        api('GET', `/api/projects/${pid}/invite-links`),
      ]);
      if (!current()) return;
      show(...head, invitationsSection(pid, invitations), inviteLinksSection(pid, links));
      return;
    }
    if (tab === 'activity') {
      const { events } = await api('GET', `/api/projects/${pid}/audit?limit=50`);
      if (!current()) return;
      show(...head, auditSection(events, names));
      return;
    }
    show(
      ...head,
      membersSection(pid, project, members),
      manager ? addMemberSection(pid) : h('p', { class: 'muted', text: 'Only the Project’s Owner and Admins can manage its members and invitations.' }),
      leaveSection(pid, project, members),
    );
  }

  function memberName(m) {
    return m.display_name || 'Unnamed user';
  }

  function membersSection(pid, project, members) {
    const manager = MANAGERS.includes(project.role);
    const me = state.session.user_id;
    const headers = ['Member', 'Role', 'Added', ''];
    const search = h('input', { type: 'search', id: 'member-search', autocomplete: 'off', placeholder: 'Name or user ID' });
    const body = h('div');
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const shown = members.filter((m) => !q || memberName(m).toLowerCase().includes(q) || m.user_id.startsWith(q));
      const rows = shown.map((m) => {
        const self = m.user_id === me;
        const name = memberName(m);
        const actions = [];
        let roleCell = title(m.role);
        if (m.role !== 'owner' && manager && !self) {
          const select = roleSelect('role', m.role);
          select.setAttribute('aria-label', `New role for ${name} (${m.user_id})`);
          const form = h(
            'form',
            {
              class: 'row-inline',
              on: {
                submit: (event) => {
                  event.preventDefault();
                  change(form, () => api('POST', `/api/projects/${pid}/members/${m.user_id}/role`, { role: select.value }), `${name} is now ${title(select.value)}.`);
                },
              },
            },
            select,
            h('button', { type: 'submit', class: 'secondary sm', 'aria-label': `Change role of ${name}`, text: 'Change role' }),
          );
          roleCell = form;
          actions.push(
            h('button', {
              type: 'button',
              class: 'danger sm',
              'aria-label': `Remove ${name} (${m.user_id})`,
              text: 'Remove',
              on: {
                click: async () => {
                  const ok = await confirmChange({
                    heading: `Remove ${name}?`,
                    body: [`${name} loses access to ${project.name} at once, including running work in it. You can invite them again later.`],
                    action: 'Remove member',
                  });
                  if (ok) change(null, () => api('POST', `/api/projects/${pid}/members/${m.user_id}/remove`), 'Member removed.');
                },
              },
            }),
          );
        }
        return row(headers, [
          h('span', {}, h('span', { class: 'cell-main', text: name }), self ? h('span', { class: 'muted', text: ' (you)' }) : null, h('span', { class: 'cell-sub', text: m.user_id })),
          roleCell,
          day(m.created_at),
          actions,
        ], { 'data-user': m.user_id });
      });
      body.replaceChildren(shown.length ? table('Project members', headers, rows) : h('p', { class: 'empty', text: 'No member matches.' }));
    };
    search.addEventListener('input', draw);
    draw();
    return card(
      cardHead(`Members (${members.length})`, 'members-h'),
      members.length > 6 ? h('div', { class: 'toolbar', role: 'search' }, field('Search members', search)) : null,
      body,
    );
  }

  function leaveSection(pid, project, members) {
    const me = members.find((m) => m.user_id === state.session.user_id);
    if (!me || me.role === 'owner') return null;
    return card(
      cardHead('Leave this Project', 'leave-h'),
      h('p', { class: 'muted', text: 'You lose access to the Project and can rejoin only with a new invitation.' }),
      h('button', {
        type: 'button',
        class: 'danger',
        text: 'Leave Project',
        on: {
          click: async () => {
            const ok = await confirmChange({ heading: `Leave ${project.name}?`, body: ['You will lose access to it, including running work in it.'], action: 'Leave Project' });
            if (!ok) return;
            try {
              await api('POST', `/api/projects/${pid}/members/${me.user_id}/remove`);
            } catch (error) {
              if (error instanceof ApiError && error.status === 401) return;
              announce(describe(error), 'bad');
              await route();
              return;
            }
            state.flash = 'You left the Project.';
            setCurrent(null);
            location.hash = '#/projects';
          },
        },
      }),
    );
  }

  function addMemberSection(pid) {
    const input = h('input', { name: 'user_id', required: true, autocomplete: 'off', class: 'mono', pattern: UUID_RE.source.slice(1, -1), spellcheck: 'false' });
    const form = h(
      'form',
      {
        on: {
          submit: (event) => {
            event.preventDefault();
            const v = values(form);
            change(form, () => api('POST', `/api/projects/${pid}/members`, { user_id: v.user_id.trim(), role: v.role }), 'Member added.');
          },
        },
      },
      h('div', { class: 'form-grid' }, field('User ID', input, 'The Nest user ID of someone who has already signed in. They find it in their account menu.'), field('Role', roleSelect('role', 'member'))),
      formError(),
      h('div', { class: 'actions-row' }, h('button', { type: 'submit', text: 'Add member' })),
    );
    return card(
      cardHead('Add an existing user', 'add-h'),
      h('p', { class: 'muted' }, 'Usually easier: ', h('a', { href: `#/projects/${pid}/invitations`, text: 'invite people by email or with an Invite Link' }), '.'),
      form,
    );
  }

  function shownOnce(label, link) {
    const id = uid('once');
    return h(
      'div',
      { class: 'shown-once', role: 'alert' },
      h('p', {}, h('strong', { text: label }), ' It is shown only once: copy it now and share it only with the people it is for.'),
      h('label', { class: 'sr-only', for: id, text: label }),
      h('input', { type: 'text', id, readonly: true, value: link, class: 'mono', 'aria-label': label }),
    );
  }

  function invitationsSection(pid, invitations) {
    const out = h('div', { id: 'invite-out' });
    const form = h(
      'form',
      {
        on: {
          submit: async (event) => {
            event.preventDefault();
            const v = values(form);
            const issued = await change(
              form,
              () => api('POST', `/api/projects/${pid}/invitations`, { email: v.email.trim(), role: v.role, send_email: v.send_email === 'on' }),
            );
            if (issued) {
              announce(issued.emailed ? 'Invitation sent.' : 'Invitation created.', 'ok');
              document.getElementById('invite-out').replaceChildren(shownOnce('Invitation link.', issued.link));
            }
          },
        },
      },
      h('div', { class: 'form-grid' }, field('Email address', h('input', { name: 'email', type: 'email', required: true, autocomplete: 'off' })), field('Role', roleSelect('role', 'member'))),
      checkbox('send_email', 'Email the invitation', true),
      formError(),
      h('div', { class: 'actions-row' }, h('button', { type: 'submit', text: 'Invite' })),
    );
    const headers = ['Email', 'Role', 'Status', 'Expires', ''];
    const rows = invitations.map((i) =>
      row(headers, [
        i.email,
        title(i.role),
        i.delivery ? `${title(i.status)} (email ${i.delivery.status})` : title(i.status),
        day(i.expires_at),
        [
          h('button', {
            type: 'button',
            class: 'secondary sm',
            text: 'Resend',
            'aria-label': `Resend invitation to ${i.email}`,
            on: {
              click: async () => {
                const issued = await change(null, () => api('POST', `/api/projects/${pid}/invitations/${i.id}/resend`, {}));
                if (issued) {
                  announce('Invitation renewed.', 'ok');
                  document.getElementById('invite-out').replaceChildren(shownOnce('New invitation link.', issued.link));
                }
              },
            },
          }),
          h('button', {
            type: 'button',
            class: 'danger sm',
            text: 'Revoke',
            'aria-label': `Revoke invitation to ${i.email}`,
            on: {
              click: async () => {
                const ok = await confirmChange({ heading: 'Revoke this invitation?', body: [`The link sent to ${i.email} stops working. They can be invited again.`], action: 'Revoke invitation' });
                if (ok) change(null, () => api('POST', `/api/projects/${pid}/invitations/${i.id}/revoke`), 'Invitation revoked.');
              },
            },
          }),
        ],
      ]),
    );
    return card(
      cardHead('Email invitations', 'inv-h'),
      h('p', { class: 'muted', text: 'The person accepts by signing in with the Google account of that address. Joining grants a Project role only, never HPC access.' }),
      form,
      out,
      h('h3', { text: 'Pending' }),
      invitations.length ? table('Pending email invitations', headers, rows) : h('p', { class: 'muted', text: 'No pending invitations.' }),
    );
  }

  function inviteLinksSection(pid, links) {
    const form = h(
      'form',
      {
        on: {
          submit: async (event) => {
            event.preventDefault();
            const v = values(form);
            const created = await change(form, () => api('POST', `/api/projects/${pid}/invite-links`, { role: v.role, max_uses: Number(v.max_uses) }));
            if (created) {
              announce('Invite Link created.', 'ok');
              document.getElementById('link-out').replaceChildren(shownOnce('Invite Link.', created.link));
            }
          },
        },
      },
      h('div', { class: 'form-grid' }, field('Role', roleSelect('role', 'viewer')), field('Uses', h('input', { name: 'max_uses', type: 'number', min: 1, max: 1000, value: '1', required: true }), 'How many people can join with this link.')),
      formError(),
      h('div', { class: 'actions-row' }, h('button', { type: 'submit', text: 'Create Invite Link' })),
    );
    const out = h('div', { id: 'link-out' });
    const headers = ['Role', 'Used', 'Status', 'Expires', ''];
    const rows = links.map((l) =>
      row(headers, [
        title(l.role),
        `${l.use_count} / ${l.max_uses}`,
        title(l.status),
        day(l.expires_at),
        h('button', {
          type: 'button',
          class: 'danger sm',
          text: 'Revoke',
          'aria-label': `Revoke Invite Link ${l.id}`,
          on: {
            click: async () => {
              const ok = await confirmChange({ heading: 'Revoke this Invite Link?', body: ['Nobody else can join with it. People who already joined keep their role.'], action: 'Revoke link' });
              if (ok) change(null, () => api('POST', `/api/projects/${pid}/invite-links/${l.id}/revoke`), 'Invite Link revoked.');
            },
          },
        }),
      ]),
    );
    return card(
      cardHead('Invite Links', 'links-h'),
      h('p', { class: 'muted', text: 'Anyone with the link who signs in can join, up to the number of uses.' }),
      form,
      out,
      h('h3', { text: 'Active' }),
      links.length ? table('Invite Links', headers, rows) : h('p', { class: 'muted', text: 'No active Invite Links.' }),
    );
  }

  const EVENTS = {
    project_created: 'Project created',
    member_added: 'Member added',
    member_removed: 'Member removed',
    member_left: 'Member left',
    role_changed: 'Role changed',
    ownership_transferred: 'Ownership transferred',
    project_renamed: 'Project renamed',
  };

  function auditSection(events, names) {
    const who = (id) => (id ? h('span', {}, names.get(id) || 'Former member', h('span', { class: 'cell-sub', text: id })) : '—');
    const headers = ['When', 'Event', 'Member', 'Role', 'By'];
    return card(
      cardHead('Recent membership changes', 'audit-h'),
      events.length
        ? table('Recent membership changes', headers, events.map((e) => row(headers, [
            when(e.occurred_at),
            EVENTS[e.event] || e.event,
            who(e.target_user_id),
            [e.old_role, e.new_role].filter(Boolean).map(title).join(' → ') || '—',
            e.actor_user_id ? who(e.actor_user_id) : e.actor_operator || '—',
          ])))
        : h('p', { class: 'muted', text: 'No changes recorded yet.' }),
    );
  }

  // Own invitations -------------------------------------------------------------------

  function tokenOf(raw) {
    const text = raw.trim();
    const match = /[#&?]token=([^&]+)/.exec(text);
    try {
      return match ? decodeURIComponent(match[1]) : text;
    } catch (_) {
      return text;
    }
  }

  function invitationsView() {
    const invitations = state.invitations || [];
    const headers = ['Project', 'Role', 'Invited by', 'Expires', ''];
    const rows = invitations.map((i) =>
      row(headers, [
        h('span', { class: 'cell-main', text: i.project_name }),
        title(i.role),
        i.invited_by_name || '—',
        day(i.expires_at),
        [
          h('button', {
            type: 'button',
            class: 'sm',
            text: 'Accept',
            'aria-label': `Accept invitation to ${i.project_name}`,
            on: { click: () => change(null, () => api('POST', `/api/invitations/${i.id}/accept`, {}), `You joined ${i.project_name}.`) },
          }),
          h('button', {
            type: 'button',
            class: 'secondary sm',
            text: 'Decline',
            'aria-label': `Decline invitation to ${i.project_name}`,
            on: {
              click: async () => {
                const ok = await confirmChange({ heading: 'Decline this invitation?', body: [`You will not join ${i.project_name}. Its admins can invite you again.`], action: 'Decline', danger: false });
                if (ok) change(null, () => api('POST', `/api/invitations/${i.id}/decline`, {}), 'Invitation declined.');
              },
            },
          }),
        ],
      ]),
    );
    const preview = h('div', { id: 'join-preview' });
    const join = h(
      'form',
      {
        on: {
          submit: async (event) => {
            event.preventDefault();
            const token = tokenOf(values(join).link);
            const error = join.querySelector('.form-error');
            error.textContent = '';
            try {
              const found = await api('POST', '/api/invitations/preview', { token });
              preview.replaceChildren(joinPreview(found, token, join));
              const first = preview.querySelector('button');
              if (first) first.focus();
            } catch (failure) {
              if (!(failure instanceof ApiError && failure.status === 401)) {
                error.textContent = describe(failure);
                join.querySelector('input').focus();
              }
            }
          },
        },
      },
      field('Invitation link', h('input', { name: 'link', required: true, autocomplete: 'off', class: 'mono', spellcheck: 'false' }), 'Paste the link from your invitation email or the Invite Link you were given.'),
      formError(),
      h('div', { class: 'actions-row' }, h('button', { type: 'submit', text: 'Review invitation' })),
    );
    show(
      h('div', { class: 'page-head' }, h('div', {}, h('h1', { text: 'Your invitations' }), h('p', { class: 'lead', text: 'Joining a Project gives you a role in it. It grants no HPC, Slurm or Provider account.' }))),
      state.invitations === null ? callout('bad', h('p', { text: 'Your invitations could not be loaded. Try again.' })) : null,
      card(
        cardHead('Pending', 'pending-h'),
        invitations.length ? table('Your pending invitations', headers, rows) : h('p', { class: 'muted', text: 'No pending invitations for your verified email address.' }),
      ),
      card(cardHead('Join with a link', 'join-h'), join, preview),
    );
  }

  function joinPreview(found, token, form) {
    const decide = (decision) =>
      change(null, async () => {
        const result = await api('POST', '/api/invitations/redeem', { token, decision });
        form.reset();
        return result;
      }, decision === 'accept' ? `You joined ${found.project_name}.` : 'Invitation declined.');
    return callout(
      '',
      h('p', {}, 'Join ', h('strong', { text: found.project_name }), ` as ${title(found.role)}?`),
      h(
        'div',
        { class: 'actions-row' },
        h('button', { type: 'button', text: 'Accept', on: { click: () => decide('accept') } }),
        h('button', { type: 'button', class: 'secondary', text: 'Decline', on: { click: () => decide('decline') } }),
      ),
    );
  }

  // Administration (G03 privileged API) -----------------------------------------------

  async function adminView(tab, current) {
    show(...loading('Administration'));
    const base = [
      h('div', { class: 'page-head' }, h('div', {}, h('h1', { text: 'Administration' }), h('p', { class: 'lead', text: 'Platform administrator actions. Each change is checked by the server and recorded with your reason. Being an administrator grants no Project membership or compute access.' }))),
      tabs('Administration sections', [['projects', '#/admin', 'Projects'], ['new', '#/admin/new', 'Create a Project'], ['users', '#/admin/users', 'Users']], tab),
    ];
    if (tab === 'new') {
      if (!current()) return;
      show(...base, createProjectSection());
      return;
    }
    if (tab === 'users') {
      const users = await api('GET', '/api/admin/users?limit=25');
      if (!current()) return;
      show(...base, userDirectory(users.users, users.next_after, null));
      return;
    }
    const projects = await api('GET', '/api/admin/projects?limit=50');
    if (!current()) return;
    show(...base, adminProjects(projects.projects, projects.next_after));
  }

  function adminProjects(first, nextAfter) {
    const all = [...first];
    const search = h('input', { type: 'search', id: 'admin-project-search', autocomplete: 'off', placeholder: 'Name or Project ID' });
    const body = h('div');
    const more = h('div');
    const headers = ['Project', 'Created', ''];
    const draw = () => {
      const q = search.value.trim().toLowerCase();
      const shown = all.filter((p) => !q || p.name.toLowerCase().includes(q) || p.id.startsWith(q));
      body.replaceChildren(
        shown.length
          ? table('Every Project', headers, shown.map((p) => row(headers, [
              h('span', {}, h('span', { class: 'cell-main', text: labelOf(p, all) }), h('span', { class: 'cell-sub', text: p.id })),
              day(p.created_at),
              h('a', { class: 'btn secondary sm', href: `#/admin/projects/${p.id}`, 'aria-label': `Manage access of ${labelOf(p, all)}`, text: 'Manage access' }),
            ])))
          : h('p', { class: 'empty', text: all.length ? 'No Project matches.' : 'No Projects yet.' }),
      );
    };
    const pageMore = (after) => {
      more.replaceChildren(after ? h('button', {
        type: 'button',
        class: 'secondary',
        text: 'Load more Projects',
        on: {
          click: async () => {
            try {
              const page = await api('GET', `/api/admin/projects?limit=50&after=${encodeURIComponent(after)}`);
              all.push(...page.projects);
              draw();
              pageMore(page.next_after);
            } catch (error) {
              if (error instanceof ApiError && error.status === 401) return;
              if (!adminRevoked(error)) announce(describe(error), 'bad');
            }
          },
        },
      }) : null);
    };
    search.addEventListener('input', draw);
    draw();
    pageMore(nextAfter);
    return card(
      cardHead('Projects', 'allp-h', h('a', { class: 'btn sm', href: '#/admin/new', text: 'Create a Project' })),
      h('div', { class: 'toolbar', role: 'search' }, field('Search Projects', search, 'Searches the Projects loaded so far.')),
      body,
      h('div', { class: 'actions-row' }, more),
    );
  }

  function createProjectSection() {
    const ownerInput = h('input', { name: 'owner_user_id', required: true, autocomplete: 'off', class: 'mono', pattern: UUID_RE.source.slice(1, -1), spellcheck: 'false' });
    const chosen = h('p', { class: 'small muted', id: 'owner-chosen', 'aria-live': 'polite' });
    const create = h(
      'form',
      {
        on: {
          submit: async (event) => {
            event.preventDefault();
            const v = values(create);
            const project = await change(create, () => api('POST', '/api/admin/projects', { name: v.name.trim(), owner_user_id: v.owner_user_id.trim(), reason: v.reason.trim() }));
            if (project) {
              state.flash = `Created ${project.name}. Its Owner has no compute access until it is approved.`;
              location.hash = `#/admin/projects/${project.id}`;
            }
          },
        },
      },
      field('Project name', h('input', { name: 'name', required: true, maxlength: 200, autocomplete: 'off' })),
      field('Initial Owner (user ID)', ownerInput, 'Find the person below and choose “Use as Owner”, or paste their user ID. You do not become a member.'),
      chosen,
      field('Reason', h('input', { name: 'reason', required: true, maxlength: 1000, autocomplete: 'off' }), 'Recorded in the security audit log.'),
      formError(),
      h('div', { class: 'actions-row' }, h('button', { type: 'submit', text: 'Create Project' })),
    );
    const pick = (user) => {
      ownerInput.value = user.id;
      chosen.textContent = `Owner: ${user.display_name || 'Unnamed user'}${user.email ? ` <${user.email}>` : ''}`;
      ownerInput.focus();
    };
    const finder = h('div');
    const email = h('input', { type: 'email', id: 'owner-email', autocomplete: 'off', placeholder: 'person@example.org' });
    const find = h(
      'form',
      {
        role: 'search',
        on: {
          submit: async (event) => {
            event.preventDefault();
            const error = find.querySelector('.form-error');
            error.textContent = '';
            try {
              const found = await api('GET', `/api/admin/users?limit=25&email=${encodeURIComponent(email.value.trim())}`);
              finder.replaceChildren(userTable(found.users, pick));
              if (!found.users.length) finder.replaceChildren(h('p', { class: 'muted', text: 'Nobody with that address has signed in to Nest yet. Ask them to sign in once, or invite them to a Project after it exists.' }));
            } catch (failure) {
              if (failure instanceof ApiError && failure.status === 401) return;
              if (!adminRevoked(failure)) error.textContent = describe(failure);
            }
          },
        },
      },
      h('div', { class: 'toolbar' }, field('Find a user by email', email), h('button', { type: 'submit', class: 'secondary', text: 'Find user' })),
      formError(),
    );
    return [
      card(cardHead('Create a Project', 'create-h'), create),
      card(cardHead('Choose the Owner', 'owner-h'), find, finder, userDirectory(null, null, pick, true)),
    ];
  }

  function userTable(users, choose) {
    const headers = ['Name', 'Email', 'Status', choose ? '' : 'User ID'];
    return table('Users', headers, users.map((u) => row(headers, [
      h('span', {}, h('span', { class: 'cell-main', text: u.display_name || '—' }), h('span', { class: 'cell-sub', text: u.id })),
      u.email || '—',
      u.status === 'active' ? badge('Active', 'ok') : badge(title(u.status), 'bad'),
      choose
        ? h('button', { type: 'button', class: 'secondary sm', text: 'Use as Owner', 'aria-label': `Use ${u.email || u.id} as Owner`, on: { click: () => choose(u) } })
        : h('span', { class: 'mono small', text: u.id }),
    ])));
  }

  // The paged user directory. With `lazy`, it loads only when opened.
  function userDirectory(users, nextAfter, choose, lazy) {
    const list = h('div');
    const more = h('div');
    const all = [];
    const append = (items, after) => {
      all.push(...items);
      list.replaceChildren(all.length ? userTable(all, choose) : h('p', { class: 'muted', text: 'No users yet.' }));
      more.replaceChildren(
        after
          ? h('button', {
              type: 'button',
              class: 'secondary',
              text: 'Load more users',
              on: {
                click: async () => {
                  try {
                    const page = await api('GET', `/api/admin/users?limit=25&after=${encodeURIComponent(after)}`);
                    append(page.users, page.next_after);
                  } catch (error) {
                    if (error instanceof ApiError && error.status === 401) return;
                    if (!adminRevoked(error)) announce(describe(error), 'bad');
                  }
                },
              },
            })
          : null,
      );
    };
    if (lazy) {
      const box = h(
        'details',
        { class: 'disclose' },
        h('summary', { text: 'Browse the user directory' }),
        list,
        h('div', { class: 'actions-row' }, more),
      );
      box.addEventListener('toggle', async () => {
        if (!box.open || all.length) return;
        try {
          const page = await api('GET', '/api/admin/users?limit=25');
          append(page.users, page.next_after);
        } catch (error) {
          if (error instanceof ApiError && error.status === 401) return;
          if (!adminRevoked(error)) announce(describe(error), 'bad');
        }
      });
      return box;
    }
    append(users, nextAfter);
    return card(cardHead('User directory', 'dir-h'), list, h('div', { class: 'actions-row' }, more));
  }

  async function accessView(pid, current) {
    show(...loading('Project access'));
    const [access, catalog] = await Promise.all([
      api('GET', `/api/admin/projects/${pid}/access`),
      api('GET', '/api/admin/platforms').catch((error) => {
        if (error instanceof ApiError && (error.status === 401 || error.reason === 'platform_admin_required')) throw error;
        return { platforms: [] };
      }),
    ]);
    if (!current()) return;
    const reason = h('input', { name: 'reason', required: true, maxlength: 1000, autocomplete: 'off', id: 'access-reason' });
    const need = () => {
      const text = reason.value.trim();
      if (!text) {
        announce('Enter a reason first. It is recorded in the security audit log.', 'bad');
        reason.setAttribute('aria-invalid', 'true');
        reason.focus();
        return null;
      }
      reason.removeAttribute('aria-invalid');
      return text;
    };
    const act = (path, body, done) => {
      const why = need();
      if (why) change(null, () => api('POST', `/api/admin/projects/${pid}/${path}`, { ...body, reason: why }), done);
    };
    const project = access.project;
    const policy = providerPolicy(access.policy);
    const decide = (path, body, done) => {
      const why = need();
      if (why) change(null, () => api('POST', `/api/admin/provider-approvals/${path}`, { ...body, project_id: pid, reason: why }), done);
    };
    const record = async (provider) => {
      if (!provider) return;
      if (!policy.recordsDecide(provider)) {
        if (!need()) return;
        const ok = await confirmChange({
          heading: 'Record an approval that is not effective here?',
          body: [`Record an approval for ${provider}? It will NOT enable submissions on this Controller: ${policy.why(provider)}`],
          action: 'Record approval',
          danger: false,
        });
        if (!ok) return;
      }
      decide('approve', { provider }, (r) => approvedMessage(r, provider));
    };
    const platformName = (id) => {
      const p = access.platforms.find((x) => x.id === id) || catalog.platforms.find((x) => x.id === id);
      return p ? `${p.display_name} (${p.slug})` : id;
    };
    const approved = access.provider_approvals.some((a) => a.status === 'approved' && policy.recordsDecide(a.provider));
    const hasDefault = access.accounts.some((l) => l.is_default);
    const requirement = (ok, text) => h('li', { class: ok ? null : 'missing' }, text, h('span', { class: 'sr-only', text: ok ? ' (in place)' : ' (missing)' }));

    const ungranted = catalog.platforms.filter((p) => p.status !== 'disabled' && !access.platforms.some((g) => g.id === p.id));
    const grantSelect = h('select', { name: 'platform', id: 'grant-platform' }, ungranted.map((p) => h('option', { value: p.slug, text: `${p.display_name} (${p.slug})` })));
    const grantInput = h('input', { name: 'platform', required: true, autocomplete: 'off', id: 'grant-platform' });
    const grantControl = ungranted.length ? grantSelect : grantInput;
    const grantForm = h(
      'form',
      { class: 'toolbar', on: { submit: (e) => { e.preventDefault(); act('platforms/grant', { platform: grantControl.value.trim() }, 'Platform granted.'); } } },
      h('div', { class: 'field' }, h('label', { for: 'grant-platform', text: 'Platform (slug or ID)' }), grantControl),
      h('button', { type: 'submit', text: 'Grant Platform' }),
    );

    const linkPlatform = access.platforms.length
      ? h('select', { name: 'platform', id: 'link-platform', required: true }, access.platforms.map((p) => h('option', { value: p.slug, text: `${p.display_name} (${p.slug})` })))
      : h('input', { name: 'platform', id: 'link-platform', required: true, autocomplete: 'off' });
    const accountForm = h(
      'form',
      { on: { submit: (e) => { e.preventDefault(); const v = values(accountForm); act('accounts/link', { platform: v.platform.trim(), account: v.account.trim(), modes: ['A'], default: v.default === 'on' }, 'Account linked.'); } } },
      h(
        'div',
        { class: 'form-grid' },
        h('div', { class: 'field' }, h('label', { for: 'link-platform', text: 'Account platform' }), linkPlatform),
        field('Slurm account', h('input', { name: 'account', required: true, autocomplete: 'off', class: 'mono' })),
      ),
      checkbox('default', 'Make it the Project’s default account (Mode A)', false),
      h('div', { class: 'actions-row' }, h('button', { type: 'submit', text: 'Link account' })),
    );

    show(
      crumbs(['#/admin', 'Administration'], [null, project.name]),
      h('div', { class: 'page-head' }, h('div', {}, h('h1', { text: `Access: ${project.name}` }), h('p', { class: 'cell-sub', text: project.id }))),
      card(
        cardHead('What running work needs', 'needs-h'),
        h('p', { class: 'muted', text: 'Running work needs every one of these, each granted separately: a Project role that may submit, an effective Provider approval, a Platform grant and a Slurm account link. No single grant authorizes compute.' }),
        h(
          'ul',
          { class: 'checklist' },
          requirement(approved, 'An effective Provider approval on this Controller'),
          requirement(access.platforms.length > 0, 'A Platform grant'),
          requirement(hasDefault, 'A default Slurm account link'),
        ),
        field('Reason for changes', reason, 'Required for every change below; recorded in the security audit log.'),
      ),
      card(
        cardHead('Platform grants', 'pf-h'),
        access.platforms.length
          ? h('ul', { class: 'next-steps' }, access.platforms.map((p) => h('li', {}, h('div', { class: 'row-inline' }, h('strong', { text: `${p.display_name} (${p.slug})` }), h('button', {
              type: 'button',
              class: 'danger sm',
              text: 'Revoke',
              'aria-label': `Revoke ${p.slug}`,
              on: {
                click: async () => {
                  if (!need()) return;
                  const ok = await confirmChange({ heading: `Revoke ${p.slug}?`, body: [`New work of ${project.name} can no longer run on ${p.display_name}.`], action: 'Revoke grant' });
                  if (ok) act('platforms/revoke', { platform: p.slug }, 'Platform grant revoked.');
                },
              },
            })))))
          : h('p', { class: 'muted', text: 'No Platform granted.' }),
        grantForm,
      ),
      card(
        cardHead('Slurm account links', 'ac-h'),
        access.accounts.length
          ? h('ul', { class: 'next-steps' }, access.accounts.map((l) => h('li', {}, h('div', { class: 'row-inline' },
              h('span', {}, h('strong', { class: 'mono', text: l.account.account_name }), ` on ${platformName(l.platform_id)} · modes ${l.modes.join(', ')}`, l.is_default ? ' · ' : '', l.is_default ? badge('Default', 'plain info') : null),
              h('button', {
                type: 'button',
                class: 'danger sm',
                text: 'Unlink',
                'aria-label': `Unlink ${l.account.account_name}`,
                on: {
                  click: async () => {
                    if (!need()) return;
                    const ok = await confirmChange({ heading: `Unlink ${l.account.account_name}?`, body: [`${project.name} can no longer charge this account${l.is_default ? '; it is the default, so work that names no account is refused' : ''}.`], action: 'Unlink account' });
                    if (ok) act('accounts/unlink', { platform: l.platform_id, account: l.account.account_name }, 'Account unlinked.');
                  },
                },
              }),
            ))))
          : h('p', { class: 'muted', text: 'No Slurm account linked.' }),
        accountForm,
      ),
      providerSection(access.provider_approvals, policy, record, async (provider) => {
        if (!need()) return;
        const ok = await confirmChange({ heading: `Revoke the ${provider} approval?`, body: ['New submissions of this Project are refused on Controllers that check this Provider.'], action: 'Revoke approval' });
        if (ok) decide('revoke', { provider }, (r) => revokedMessage(r, provider));
      }),
    );
  }

  // Provider approvals (G03) ----------------------------------------------------------
  //
  // An approval record and an effective approval are different things: the
  // serving Controller reports in `policy` where it takes approvals from. With
  // `database`, recorded approvals and revocations decide; with `environment`,
  // its environment allow-list decides what is approved and recorded approvals do
  // nothing until the cutover, but recorded revocations still refuse new
  // submissions. A record for another Provider than the one it checks, or an
  // unknown policy, decides nothing here.

  const SOURCES = {
    database: 'Database: approvals and revocations recorded here decide admission.',
    environment: 'Environment allow-list: the Controller’s configured list decides which Projects are approved. Approvals recorded here do nothing until the cutover to database mode; revocations recorded here still refuse new submissions.',
    unknown: 'Unknown: this server did not report its Provider policy. Treat recorded approvals and revocations as not effective.',
  };

  function providerPolicy(raw) {
    const p = raw || {};
    const source = Object.hasOwn(SOURCES, p.source) ? p.source : 'unknown';
    const provider = typeof p.provider === 'string' && p.provider ? p.provider : null;
    const checked = (name) => provider !== null && name === provider;
    return {
      source,
      provider,
      effective: source === 'database' && p.approvals_effective === true,
      recordsDecide(name) {
        return checked(name) && this.effective;
      },
      revocationsDecide(name) {
        return checked(name) && source !== 'unknown';
      },
      why(name) {
        if (source === 'unknown') return 'this server did not report its Provider policy.';
        if (!checked(name)) return `this Controller checks ${provider ? `Provider ${provider}` : 'no Provider'}, not ${name}.`;
        return 'this Controller takes approvals from its environment allow-list until the cutover.';
      },
    };
  }

  function approvalState(a, policy) {
    if (a.status === 'approved') {
      return policy.recordsDecide(a.provider)
        ? 'Approved: recorded and effective for new submissions on this Controller.'
        : `Approved: recorded only, not effective here (${policy.why(a.provider)})`;
    }
    if (a.status === 'revoked') {
      return policy.revocationsDecide(a.provider)
        ? 'Revoked: recorded and effective; new submissions are refused on this Controller.'
        : `Revoked: recorded only (${policy.why(a.provider)})`;
    }
    return `${title(a.status)}: recorded.`;
  }

  function approvedMessage(result, provider) {
    const policy = providerPolicy(result.policy);
    const recorded = result.changed === false ? 'An approval was already recorded.' : 'Approval recorded.';
    return policy.recordsDecide(provider)
      ? `${recorded} It is effective for new submissions on this Controller; running work still needs a Platform grant, a Slurm account and a role that may submit.`
      : `${recorded} It does not enable submissions on this Controller: ${policy.why(provider)}`;
  }

  function revokedMessage(result, provider) {
    const policy = providerPolicy(result.policy);
    const recorded = result.changed === false ? 'A revocation was already recorded.' : 'Revocation recorded.';
    return policy.revocationsDecide(provider)
      ? `${recorded} New submissions are refused on this Controller.`
      : `${recorded} It does not change admission on this Controller: ${policy.why(provider)}`;
  }

  function providerSection(approvals, policy, record, revoke) {
    const input = h('input', { name: 'provider', required: true, autocomplete: 'off', class: 'mono', value: policy.provider || '' });
    const form = h(
      'form',
      { class: 'toolbar', on: { submit: (e) => { e.preventDefault(); record(input.value.trim()); } } },
      field('Provider', input),
      h('button', { type: 'submit', text: policy.effective ? 'Approve Provider' : 'Record approval (not effective here)' }),
    );
    const headers = ['Provider', 'Record', 'Effect on this Controller', 'Updated', ''];
    return card(
      cardHead('Provider approvals', 'pv-h'),
      h(
        'dl',
        { class: 'facts', 'aria-label': 'Provider policy of this Controller' },
        h('dt', { text: 'Approval source' }), h('dd', { text: SOURCES[policy.source] }),
        h('dt', { text: 'Provider checked' }), h('dd', {}, policy.provider ? h('code', { text: policy.provider }) : 'Not reported'),
      ),
      policy.effective
        ? null
        : callout('warn', h('p', { text: 'Approving here only records an approval. It does not enable submissions on this Controller.' })),
      approvals.length
        ? table('Provider approvals', headers, approvals.map((a) =>
            row(headers, [
              h('code', { text: a.provider }),
              title(a.status),
              approvalState(a, policy),
              day(a.updated_at),
              a.status === 'approved'
                ? h('button', { type: 'button', class: 'danger sm', text: 'Revoke', 'aria-label': `Revoke ${a.provider} approval`, on: { click: () => revoke(a.provider) } })
                : null,
            ]),
          ))
        : h('p', { class: 'muted', text: 'No Provider approval recorded.' }),
      form,
    );
  }

  // Start -----------------------------------------------------------------------------

  async function start() {
    try {
      state.session = await loadSession();
    } catch (error) {
      show(h('h1', { text: 'Nest is unavailable' }), errorBox(error, start));
      return;
    }
    if (!state.session) return signedOut();
    state.admin = await loadAdmin();
    await route();
  }

  window.addEventListener('hashchange', () => {
    announce(state.flash || '', 'ok');
    state.flash = null;
    route();
  });
  // The skip link moves focus without changing the route (its #main is no page).
  document.querySelector('.skip').addEventListener('click', (event) => {
    event.preventDefault();
    main().focus();
  });
  // Closes the account menu on Escape or a click elsewhere.
  document.addEventListener('keydown', (event) => {
    const menu = document.querySelector('details.account[open]');
    if (event.key === 'Escape' && menu) {
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  });
  document.addEventListener('click', (event) => {
    const menu = document.querySelector('details.account[open]');
    if (menu && !menu.contains(event.target)) menu.open = false;
  });
  start();
})();
