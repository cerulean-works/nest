// Nest Management Console (G02a).
//
// Runs on the Controller's auth listener and calls its JSON routes with the
// browser's HttpOnly session cookies; it never sees a token, password or key.
// Every decision is the server's: this script shows only the actions a role
// allows, and reports what the server answers when a request is refused anyway.
// Content is only ever inserted as text, never as HTML.
'use strict';

(function () {
  const PRIVILEGED_API = document.documentElement.dataset.privilegedApi === 'available';
  const ASSIGNABLE = ['admin', 'member', 'viewer'];
  const MANAGERS = ['owner', 'admin'];
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const LOGIN_URL = '/auth/google/login?next=console';

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

  const state = { session: null, admin: false, render: 0, flash: null };

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

  function announce(message, kind) {
    const status = document.getElementById('status');
    status.textContent = '';
    status.className = `status ${kind || ''}`;
    if (message) status.append(h('p', { text: message }));
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

  function loading(what) {
    return h('p', { class: 'muted', 'aria-busy': 'true', text: `Loading ${what}…` });
  }

  function errorBox(error, retry) {
    return h(
      'div',
      { class: 'error', role: 'alert' },
      h('p', { text: describe(error) }),
      retry ? h('button', { type: 'button', on: { click: retry }, text: 'Try again' }) : null,
    );
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
          state.session = await parse(r);
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

  // Shell -----------------------------------------------------------------------

  function renderShell() {
    const nav = document.getElementById('nav');
    const who = document.getElementById('who');
    if (!state.session) {
      nav.replaceChildren();
      who.replaceChildren();
      return;
    }
    const links = [
      ['#/projects', 'Projects'],
      ['#/invitations', 'Invitations'],
    ];
    if (state.admin) links.push(['#/admin', 'Administration']);
    const current = location.hash.split('/')[1] || 'projects';
    nav.replaceChildren(
      h(
        'ul',
        {},
        links.map(([href, label]) =>
          h(
            'li',
            {},
            h('a', { href, 'aria-current': href === `#/${current}` ? 'page' : null, text: label }),
          ),
        ),
      ),
    );
    who.replaceChildren(
      h('span', { class: 'muted', text: `Signed in · user ${state.session.user_id}` }),
      h('button', { type: 'button', class: 'secondary', on: { click: signOut }, text: 'Sign out' }),
    );
  }

  function signedOut(message) {
    state.session = null;
    state.admin = false;
    renderShell();
    announce('');
    show(
      h('h1', { text: 'Sign in to Nest' }),
      message ? h('p', { class: 'error', role: 'alert', text: message }) : null,
      h('p', { text: 'Use your Google account to manage your Nest Projects, members and invitations.' }),
      h('p', {}, h('a', { class: 'button', href: LOGIN_URL, text: 'Sign in with Google' })),
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
    renderShell();
    if (!state.session) return signedOut();
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    try {
      if (parts.length === 0 || (parts[0] === 'projects' && parts.length === 1)) {
        return await projectsView(current);
      }
      if (parts[0] === 'projects' && parts.length === 2 && UUID_RE.test(parts[1])) {
        return await projectView(parts[1], current);
      }
      if (parts[0] === 'invitations' && parts.length === 1) return await invitationsView(current);
      if (parts[0] === 'admin' && !state.admin) return adminUnavailable();
      if (parts[0] === 'admin') {
        if (parts.length === 1) return await adminView(current);
        if (parts[1] === 'projects' && parts.length === 3 && UUID_RE.test(parts[2])) {
          return await accessView(parts[2], current);
        }
      }
      show(h('h1', { text: 'Not found' }), h('p', {}, h('a', { href: '#/projects', text: 'Back to Projects' })));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return;
      if (!current()) return;
      if (error instanceof ApiError && error.reason === 'platform_admin_required') {
        // The server no longer counts the caller as a platform admin.
        state.admin = false;
        renderShell();
        return adminUnavailable();
      }
      show(h('h1', { text: 'Something went wrong' }), errorBox(error, route));
    }
  }

  function adminUnavailable() {
    show(
      h('h1', { text: 'Administration not available' }),
      h('p', { class: 'error', role: 'alert', text: REASONS.platform_admin_required }),
      h('p', {}, h('a', { href: '#/projects', text: 'Back to your Projects' })),
    );
  }

  // A change: runs it, reports the outcome and re-reads the page, since what the
  // caller may do can have changed (a role change, a removal, a revoked session).
  async function change(form, run, done) {
    const error = form ? form.querySelector('.form-error') : null;
    const buttons = form ? form.querySelectorAll('button') : [];
    if (error) error.textContent = '';
    buttons.forEach((b) => (b.disabled = true));
    try {
      const result = await run();
      if (done) announce(typeof done === 'function' ? done(result) : done, 'ok');
      await route();
      return result;
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) return null;
      const message = describe(failure);
      if (error) error.textContent = message;
      announce(message, 'bad');
      if (failure instanceof ApiError && [403, 404, 410].includes(failure.status)) await route();
      return null;
    } finally {
      buttons.forEach((b) => (b.disabled = false));
    }
  }

  function formError() {
    return h('p', { class: 'form-error', role: 'alert' });
  }

  function values(form) {
    return Object.fromEntries(new FormData(form).entries());
  }

  // Projects ------------------------------------------------------------------------

  async function projectsView(current) {
    show(h('h1', { text: 'Your Projects' }), loading('Projects'));
    const { projects } = await api('GET', '/api/projects');
    if (!current()) return;
    const body = projects.length
      ? h(
          'table',
          {},
          h('caption', { class: 'sr-only', text: 'Projects you are a member of' }),
          h('thead', {}, h('tr', {}, h('th', { scope: 'col', text: 'Name' }), h('th', { scope: 'col', text: 'Your role' }), h('th', { scope: 'col', text: 'Created' }))),
          h(
            'tbody',
            {},
            projects.map((p) =>
              h(
                'tr',
                {},
                h('td', {}, h('a', { href: `#/projects/${p.id}`, text: p.name })),
                h('td', { text: title(p.role) }),
                h('td', { text: when(p.created_at) }),
              ),
            ),
          ),
        )
      : h(
          'div',
          { class: 'empty' },
          h('p', { text: 'You are not a member of any Project yet.' }),
          h('p', {}, 'Ask a Project Owner or Admin to invite you, then accept the invitation under ', h('a', { href: '#/invitations', text: 'Invitations' }), '.'),
        );
    show(
      h('h1', { text: 'Your Projects' }),
      body,
      h('p', { class: 'muted', text: 'New Projects are created by a platform administrator, who names their Owner.' }),
    );
  }

  async function projectView(pid, current) {
    show(h('h1', { text: 'Project' }), loading('the Project'));
    let project, members;
    try {
      [project, { members }] = await Promise.all([
        api('GET', `/api/projects/${pid}`),
        api('GET', `/api/projects/${pid}/members`),
      ]);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        if (current()) {
          show(
            h('h1', { text: 'Project not available' }),
            h('p', { class: 'error', role: 'alert', text: ERRORS.not_found }),
            h('p', {}, h('a', { href: '#/projects', text: 'Back to your Projects' })),
          );
        }
        return;
      }
      throw error;
    }
    if (!current()) return;
    const role = project.role;
    const manager = MANAGERS.includes(role);
    let invitations = [], links = [], events = [];
    if (manager) {
      [{ invitations }, { invite_links: links }, { events }] = await Promise.all([
        api('GET', `/api/projects/${pid}/invitations`),
        api('GET', `/api/projects/${pid}/invite-links`),
        api('GET', `/api/projects/${pid}/audit?limit=20`),
      ]);
      if (!current()) return;
    }
    show(
      h('p', {}, h('a', { href: '#/projects', text: '← Your Projects' })),
      h('h1', { text: project.name }),
      h(
        'dl',
        { class: 'facts' },
        h('dt', { text: 'Project ID' }), h('dd', {}, h('code', { text: project.id })),
        h('dt', { text: 'Your role' }), h('dd', { text: title(role) }),
        h('dt', { text: 'Owner' }), h('dd', {}, h('code', { text: project.owner_user_id })),
        h('dt', { text: 'Created' }), h('dd', { text: when(project.created_at) }),
      ),
      membersSection(pid, role, members),
      manager ? addMemberSection(pid) : null,
      manager ? invitationsSection(pid, invitations) : null,
      manager ? inviteLinksSection(pid, links) : null,
      manager ? auditSection(events) : null,
      manager ? null : h('p', { class: 'muted', text: 'Only the Project’s Owner and Admins can manage its members and invitations.' }),
    );
  }

  function membersSection(pid, role, members) {
    const manager = MANAGERS.includes(role);
    const me = state.session.user_id;
    const rows = members.map((m) => {
      const self = m.user_id === me;
      const actions = [];
      if (m.role !== 'owner' && manager && !self) {
        const select = roleSelect('role', m.role);
        select.setAttribute('aria-label', `New role for ${m.user_id}`);
        const form = h(
          'form',
          {
            class: 'inline',
            on: {
              submit: (event) => {
                event.preventDefault();
                change(form, () => api('POST', `/api/projects/${pid}/members/${m.user_id}/role`, { role: select.value }), 'Role changed.');
              },
            },
          },
          select,
          h('button', { type: 'submit', text: 'Change role' }),
        );
        actions.push(form);
        actions.push(
          h('button', {
            type: 'button',
            class: 'danger',
            'aria-label': `Remove ${m.user_id}`,
            text: 'Remove',
            on: {
              click: () => {
                if (window.confirm('Remove this member from the Project?')) {
                  change(null, () => api('POST', `/api/projects/${pid}/members/${m.user_id}/remove`), 'Member removed.');
                }
              },
            },
          }),
        );
      } else if (self && m.role !== 'owner') {
        actions.push(
          h('button', {
            type: 'button',
            class: 'danger',
            text: 'Leave Project',
            on: {
              click: async () => {
                if (!window.confirm('Leave this Project? You will lose access to it.')) return;
                try {
                  await api('POST', `/api/projects/${pid}/members/${m.user_id}/remove`);
                } catch (error) {
                  if (error instanceof ApiError && error.status === 401) return;
                  announce(describe(error), 'bad');
                  await route();
                  return;
                }
                state.flash = 'You left the Project.';
                location.hash = '#/projects';
              },
            },
          }),
        );
      }
      return h(
        'tr',
        {},
        h('td', {}, h('code', { text: m.user_id }), self ? ' (you)' : ''),
        h('td', { text: title(m.role) }),
        h('td', { text: when(m.created_at) }),
        h('td', { class: 'actions' }, actions),
      );
    });
    return h(
      'section',
      { 'aria-labelledby': 'members-h' },
      h('h2', { id: 'members-h', text: `Members (${members.length})` }),
      h(
        'table',
        {},
        h('thead', {}, h('tr', {}, ['User', 'Role', 'Added', 'Actions'].map((t) => h('th', { scope: 'col', text: t })))),
        h('tbody', {}, rows),
      ),
    );
  }

  function addMemberSection(pid) {
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
      field('User ID', h('input', { name: 'user_id', required: true, autocomplete: 'off', pattern: UUID_RE.source.slice(1, -1), spellcheck: 'false' }), 'The Nest user ID of someone who has already signed in.'),
      field('Role', roleSelect('role', 'member')),
      formError(),
      h('button', { type: 'submit', text: 'Add member' }),
    );
    return h('section', { 'aria-labelledby': 'add-h' }, h('h2', { id: 'add-h', text: 'Add an existing user' }), form);
  }

  function shownOnce(label, link) {
    return h(
      'div',
      { class: 'notice', role: 'alert' },
      h('p', { text: `${label} It is shown only once: copy it now and share it only with the people it is for.` }),
      h('input', { type: 'text', readonly: true, value: link, 'aria-label': label, class: 'wide' }),
    );
  }

  function invitationsSection(pid, invitations) {
    const out = h('div');
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
      field('Email address', h('input', { name: 'email', type: 'email', required: true, autocomplete: 'off' })),
      field('Role', roleSelect('role', 'member')),
      h('div', { class: 'field check' }, h('input', { type: 'checkbox', name: 'send_email', id: 'send-email', checked: true }), h('label', { for: 'send-email', text: 'Email the invitation' })),
      formError(),
      h('button', { type: 'submit', text: 'Invite' }),
    );
    out.id = 'invite-out';
    const rows = invitations.map((i) =>
      h(
        'tr',
        {},
        h('td', { text: i.email }),
        h('td', { text: title(i.role) }),
        h('td', { text: i.delivery ? `${i.status} (email ${i.delivery.status})` : i.status }),
        h('td', { text: when(i.expires_at) }),
        h(
          'td',
          { class: 'actions' },
          h('button', {
            type: 'button',
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
            class: 'danger',
            text: 'Revoke',
            'aria-label': `Revoke invitation to ${i.email}`,
            on: {
              click: () => {
                if (window.confirm(`Revoke the invitation to ${i.email}?`)) {
                  change(null, () => api('POST', `/api/projects/${pid}/invitations/${i.id}/revoke`), 'Invitation revoked.');
                }
              },
            },
          }),
        ),
      ),
    );
    return h(
      'section',
      { 'aria-labelledby': 'inv-h' },
      h('h2', { id: 'inv-h', text: 'Email invitations' }),
      form,
      out,
      invitations.length
        ? h('table', {}, h('thead', {}, h('tr', {}, ['Email', 'Role', 'Status', 'Expires', 'Actions'].map((t) => h('th', { scope: 'col', text: t })))), h('tbody', {}, rows))
        : h('p', { class: 'muted', text: 'No pending invitations.' }),
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
      field('Role', roleSelect('role', 'viewer')),
      field('Uses', h('input', { name: 'max_uses', type: 'number', min: 1, max: 1000, value: '1', required: true }), 'How many people can join with this link.'),
      formError(),
      h('button', { type: 'submit', text: 'Create Invite Link' }),
    );
    const out = h('div', { id: 'link-out' });
    const rows = links.map((l) =>
      h(
        'tr',
        {},
        h('td', { text: title(l.role) }),
        h('td', { text: `${l.use_count} / ${l.max_uses}` }),
        h('td', { text: l.status }),
        h('td', { text: when(l.expires_at) }),
        h(
          'td',
          { class: 'actions' },
          h('button', {
            type: 'button',
            class: 'danger',
            text: 'Revoke',
            'aria-label': `Revoke Invite Link ${l.id}`,
            on: {
              click: () => {
                if (window.confirm('Revoke this Invite Link?')) {
                  change(null, () => api('POST', `/api/projects/${pid}/invite-links/${l.id}/revoke`), 'Invite Link revoked.');
                }
              },
            },
          }),
        ),
      ),
    );
    return h(
      'section',
      { 'aria-labelledby': 'links-h' },
      h('h2', { id: 'links-h', text: 'Invite Links' }),
      form,
      out,
      links.length
        ? h('table', {}, h('thead', {}, h('tr', {}, ['Role', 'Used', 'Status', 'Expires', 'Actions'].map((t) => h('th', { scope: 'col', text: t })))), h('tbody', {}, rows))
        : h('p', { class: 'muted', text: 'No active Invite Links.' }),
    );
  }

  function auditSection(events) {
    return h(
      'section',
      { 'aria-labelledby': 'audit-h' },
      h('h2', { id: 'audit-h', text: 'Recent membership changes' }),
      events.length
        ? h(
            'table',
            {},
            h('thead', {}, h('tr', {}, ['When', 'Event', 'Member', 'Role', 'By'].map((t) => h('th', { scope: 'col', text: t })))),
            h(
              'tbody',
              {},
              events.map((e) =>
                h(
                  'tr',
                  {},
                  h('td', { text: when(e.occurred_at) }),
                  h('td', { text: e.event }),
                  h('td', {}, e.target_user_id ? h('code', { text: e.target_user_id }) : '—'),
                  h('td', { text: [e.old_role, e.new_role].filter(Boolean).join(' → ') || '—' }),
                  h('td', {}, e.actor_user_id ? h('code', { text: e.actor_user_id }) : e.actor_operator || '—'),
                ),
              ),
            ),
          )
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

  async function invitationsView(current) {
    show(h('h1', { text: 'Your invitations' }), loading('invitations'));
    const { invitations } = await api('GET', '/api/invitations');
    if (!current()) return;
    const rows = invitations.map((i) =>
      h(
        'tr',
        {},
        h('td', { text: i.project_name }),
        h('td', { text: title(i.role) }),
        h('td', { text: i.invited_by_name || '—' }),
        h('td', { text: when(i.expires_at) }),
        h(
          'td',
          { class: 'actions' },
          h('button', {
            type: 'button',
            text: 'Accept',
            'aria-label': `Accept invitation to ${i.project_name}`,
            on: { click: () => change(null, () => api('POST', `/api/invitations/${i.id}/accept`, {}), `You joined ${i.project_name}.`) },
          }),
          h('button', {
            type: 'button',
            class: 'secondary',
            text: 'Decline',
            'aria-label': `Decline invitation to ${i.project_name}`,
            on: { click: () => change(null, () => api('POST', `/api/invitations/${i.id}/decline`, {}), 'Invitation declined.') },
          }),
        ),
      ),
    );
    const preview = h('div', { id: 'join-preview' });
    const join = h(
      'form',
      {
        on: {
          submit: async (event) => {
            event.preventDefault();
            const token = tokenOf(values(join).link);
            join.querySelector('.form-error').textContent = '';
            try {
              const found = await api('POST', '/api/invitations/preview', { token });
              preview.replaceChildren(joinPreview(found, token, join));
            } catch (error) {
              if (!(error instanceof ApiError && error.status === 401)) join.querySelector('.form-error').textContent = describe(error);
            }
          },
        },
      },
      field('Invitation link', h('input', { name: 'link', required: true, autocomplete: 'off', class: 'wide', spellcheck: 'false' }), 'Paste the link from your invitation email or the Invite Link you were given.'),
      formError(),
      h('button', { type: 'submit', text: 'Review invitation' }),
    );
    show(
      h('h1', { text: 'Your invitations' }),
      h(
        'section',
        { 'aria-labelledby': 'pending-h' },
        h('h2', { id: 'pending-h', text: 'Pending' }),
        invitations.length
          ? h('table', {}, h('thead', {}, h('tr', {}, ['Project', 'Role', 'Invited by', 'Expires', 'Actions'].map((t) => h('th', { scope: 'col', text: t })))), h('tbody', {}, rows))
          : h('p', { class: 'muted', text: 'No pending invitations for your verified email address.' }),
      ),
      h('section', { 'aria-labelledby': 'join-h' }, h('h2', { id: 'join-h', text: 'Join with a link' }), join, preview),
    );
  }

  function joinPreview(found, token, form) {
    const decide = (decision) =>
      change(null, async () => {
        const result = await api('POST', '/api/invitations/redeem', { token, decision });
        form.reset();
        return result;
      }, decision === 'accept' ? `You joined ${found.project_name}.` : 'Invitation declined.');
    return h(
      'div',
      { class: 'notice' },
      h('p', {}, 'Join ', h('strong', { text: found.project_name }), ` as ${title(found.role)}?`),
      h('button', { type: 'button', text: 'Accept', on: { click: () => decide('accept') } }),
      h('button', { type: 'button', class: 'secondary', text: 'Decline', on: { click: () => decide('decline') } }),
    );
  }

  // Administration (G03 privileged API) -----------------------------------------------

  async function adminView(current) {
    show(h('h1', { text: 'Administration' }), loading('the user directory'));
    const [users, projects] = await Promise.all([
      api('GET', '/api/admin/users?limit=25'),
      api('GET', '/api/admin/projects?limit=25'),
    ]);
    if (!current()) return;
    const ownerInput = h('input', { name: 'owner_user_id', required: true, autocomplete: 'off', pattern: UUID_RE.source.slice(1, -1), spellcheck: 'false' });
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
      field('Initial Owner (user ID)', ownerInput, 'Pick a user below. You do not become a member.'),
      field('Reason', h('input', { name: 'reason', required: true, maxlength: 1000, autocomplete: 'off' }), 'Recorded in the security audit log.'),
      formError(),
      h('button', { type: 'submit', text: 'Create Project' }),
    );
    const directory = userDirectory(users.users, users.next_after, (id) => {
      ownerInput.value = id;
      ownerInput.focus();
    });
    show(
      h('h1', { text: 'Administration' }),
      h('p', { class: 'muted', text: 'Platform administrator actions. Each change is checked by the server and recorded with your reason. Being an administrator grants no Project membership or compute access.' }),
      h('section', { 'aria-labelledby': 'create-h' }, h('h2', { id: 'create-h', text: 'Create a Project' }), create),
      directory,
      h(
        'section',
        { 'aria-labelledby': 'allp-h' },
        h('h2', { id: 'allp-h', text: 'Projects' }),
        projects.projects.length
          ? h('ul', {}, projects.projects.map((p) => h('li', {}, h('a', { href: `#/admin/projects/${p.id}`, text: p.name }), ' ', h('code', { text: p.id }))))
          : h('p', { class: 'muted', text: 'No Projects yet.' }),
      ),
    );
  }

  function userDirectory(users, nextAfter, choose) {
    const list = h('tbody');
    const more = h('div');
    const append = (items, after) => {
      list.append(
        ...items.map((u) =>
          h(
            'tr',
            {},
            h('td', { text: u.display_name || '—' }),
            h('td', { text: u.email || '—' }),
            h('td', { text: u.status }),
            h('td', {}, h('code', { text: u.id })),
            h('td', {}, h('button', { type: 'button', class: 'secondary', text: 'Use as Owner', 'aria-label': `Use ${u.email || u.id} as Owner`, on: { click: () => choose(u.id) } })),
          ),
        ),
      );
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
                    if (!(error instanceof ApiError && error.status === 401)) announce(describe(error), 'bad');
                  }
                },
              },
            })
          : null,
      );
    };
    append(users, nextAfter);
    return h(
      'section',
      { 'aria-labelledby': 'dir-h' },
      h('h2', { id: 'dir-h', text: 'User directory' }),
      h('table', {}, h('thead', {}, h('tr', {}, ['Name', 'Email', 'Status', 'User ID', ''].map((t) => h('th', { scope: 'col', text: t })))), list),
      more,
    );
  }

  async function accessView(pid, current) {
    show(h('h1', { text: 'Project access' }), loading('access'));
    const access = await api('GET', `/api/admin/projects/${pid}/access`);
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
    const textForm = (label, name, submit, button) => {
      const form = h(
        'form',
        { class: 'inline', on: { submit: (e) => { e.preventDefault(); submit(values(form)[name].trim()); } } },
        field(label, h('input', { name, required: true, autocomplete: 'off' })),
        h('button', { type: 'submit', text: button }),
      );
      return form;
    };
    const project = access.project;
    const policy = providerPolicy(access.policy);
    const decide = (path, body, done) => {
      const why = need();
      if (why) change(null, () => api('POST', `/api/admin/provider-approvals/${path}`, { ...body, project_id: pid, reason: why }), done);
    };
    const record = (provider) => {
      if (!provider) return;
      if (!policy.recordsDecide(provider)) {
        const ok = window.confirm(
          `Record an approval for ${provider}? It will NOT enable submissions on this Controller: ${policy.why(provider)}`,
        );
        if (!ok) return;
      }
      decide('approve', { provider }, (r) => approvedMessage(r, provider));
    };
    show(
      h('p', {}, h('a', { href: '#/admin', text: '← Administration' })),
      h('h1', { text: `Access: ${project.name}` }),
      h('dl', { class: 'facts' }, h('dt', { text: 'Project ID' }), h('dd', {}, h('code', { text: project.id })), h('dt', { text: 'Owner' }), h('dd', {}, h('code', { text: project.owner_user_id }))),
      h('p', { class: 'muted', text: 'Running work needs every one of these, each granted separately: a Project role that may submit, an effective Provider approval, a Platform grant and a Slurm account link. No single grant authorizes compute.' }),
      field('Reason for changes', reason, 'Required for every change below; recorded in the security audit log.'),
      h(
        'section',
        { 'aria-labelledby': 'pf-h' },
        h('h2', { id: 'pf-h', text: 'Platform grants' }),
        access.platforms.length
          ? h('ul', {}, access.platforms.map((p) => h('li', {}, `${p.display_name} (${p.slug}) `, h('button', { type: 'button', class: 'danger', text: 'Revoke', 'aria-label': `Revoke ${p.slug}`, on: { click: () => act('platforms/revoke', { platform: p.slug }, 'Platform grant revoked.') } }))))
          : h('p', { class: 'muted', text: 'No Platform granted.' }),
        textForm('Platform (slug or ID)', 'platform', (platform) => act('platforms/grant', { platform }, 'Platform granted.'), 'Grant Platform'),
      ),
      h(
        'section',
        { 'aria-labelledby': 'ac-h' },
        h('h2', { id: 'ac-h', text: 'Slurm account links' }),
        access.accounts.length
          ? h('ul', {}, access.accounts.map((l) => h('li', {}, `${l.account.account_name} on ${l.platform_id} (modes ${l.modes.join(', ')}${l.is_default ? ', default' : ''}) `, h('button', { type: 'button', class: 'danger', text: 'Unlink', 'aria-label': `Unlink ${l.account.account_name}`, on: { click: () => act('accounts/unlink', { platform: l.platform_id, account: l.account.account_name }, 'Account unlinked.') } }))))
          : h('p', { class: 'muted', text: 'No Slurm account linked.' }),
        accountForm((v) => act('accounts/link', { platform: v.platform.trim(), account: v.account.trim(), modes: ['A'], default: v.default === 'on' }, 'Account linked.')),
      ),
      providerSection(access.provider_approvals, policy, record, (provider) =>
        decide('revoke', { provider }, (r) => revokedMessage(r, provider)),
      ),
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
    const input = h('input', { name: 'provider', required: true, autocomplete: 'off', value: policy.provider || '' });
    const form = h(
      'form',
      { class: 'inline', on: { submit: (e) => { e.preventDefault(); record(input.value.trim()); } } },
      field('Provider', input),
      h('button', { type: 'submit', text: policy.effective ? 'Approve Provider' : 'Record approval (not effective here)' }),
    );
    return h(
      'section',
      { 'aria-labelledby': 'pv-h' },
      h('h2', { id: 'pv-h', text: 'Provider approvals' }),
      h(
        'dl',
        { class: 'facts', 'aria-label': 'Provider policy of this Controller' },
        h('dt', { text: 'Approval source' }), h('dd', { text: SOURCES[policy.source] }),
        h('dt', { text: 'Provider checked' }), h('dd', {}, policy.provider ? h('code', { text: policy.provider }) : 'Not reported'),
      ),
      policy.effective
        ? null
        : h('p', { class: 'notice', text: 'Approving here only records an approval. It does not enable submissions on this Controller.' }),
      approvals.length
        ? h(
            'table',
            {},
            h('thead', {}, h('tr', {}, ['Provider', 'Record', 'Effect on this Controller', 'Updated', ''].map((t) => h('th', { scope: 'col', text: t })))),
            h(
              'tbody',
              {},
              approvals.map((a) =>
                h(
                  'tr',
                  {},
                  h('td', {}, h('code', { text: a.provider })),
                  h('td', { text: title(a.status) }),
                  h('td', { text: approvalState(a, policy) }),
                  h('td', { text: when(a.updated_at) }),
                  h(
                    'td',
                    {},
                    a.status === 'approved'
                      ? h('button', { type: 'button', class: 'danger', text: 'Revoke', 'aria-label': `Revoke ${a.provider} approval`, on: { click: () => revoke(a.provider) } })
                      : null,
                  ),
                ),
              ),
            ),
          )
        : h('p', { class: 'muted', text: 'No Provider approval recorded.' }),
      form,
    );
  }

  function accountForm(submit) {
    const form = h(
      'form',
      { on: { submit: (e) => { e.preventDefault(); submit(values(form)); } } },
      field('Platform (slug or ID)', h('input', { name: 'platform', required: true, autocomplete: 'off' })),
      field('Slurm account', h('input', { name: 'account', required: true, autocomplete: 'off' })),
      h('div', { class: 'field check' }, h('input', { type: 'checkbox', name: 'default', id: 'acct-default' }), h('label', { for: 'acct-default', text: 'Make it the Project’s default account (Mode A)' })),
      h('button', { type: 'submit', text: 'Link account' }),
    );
    return form;
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
  start();
})();
