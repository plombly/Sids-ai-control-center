// Settings → Users (administrators): who uses this LAIka and what each person
// may do (apps/api/users_routes.py, access.py). People join with a one-time
// invite link and choose their own password.
import { requestJSON } from './api.js';
import { esc, escValue } from './format.js';

export const LEVEL_LABELS = { '': 'No access', view: 'View', build: 'Build', approve: 'Approve' };
const LEVEL_HELP = 'View: see everything in the project. Build: also give goals, edit files, build and preview. Approve: also approve changes, undo, and the project\'s settings and secrets. Access to a parent project covers its children.';

// Projects people can be given: not the ones the LAIka builder manages.
export const grantable = (projects = []) =>
  projects.filter(project => project.id !== 'laika' && !project.view_only).sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id)));

const when = seconds => (seconds ? new Date(seconds * 1000).toLocaleString() : 'never');

export function accessSummary(user, projects = []) {
  if (user.role === 'admin') return 'Everything (administrator)';
  const names = new Map(projects.map(project => [project.id, project.name || project.id]));
  const grants = Object.entries(user.access || {});
  if (!grants.length) return 'No projects yet';
  return grants.map(([id, level]) => `${names.get(id) || id}: ${LEVEL_LABELS[level] || level}`).join(', ');
}

export function accessFields(projects = [], access = {}, prefix = 'access', hidden = false) {
  const rows = grantable(projects)
    .map(project => {
      const indent = project.parent ? ' ↳ ' : '';
      const options = Object.entries(LEVEL_LABELS)
        .map(([value, label]) => `<option value="${escValue(value)}"${(access[project.id] || '') === value ? ' selected' : ''}>${esc(label)}</option>`)
        .join('');
      return `<label class="grant-row"><span>${indent}${esc(project.name || project.id)}</span><select name="${escValue(prefix)}:${escValue(project.id)}">${options}</select></label>`;
    })
    .join('');
  return `<div class="grant-list" data-member-only${hidden ? ' hidden' : ''}>${rows || '<p class="subtle">No projects yet.</p>'}</div><p class="subtle">${esc(LEVEL_HELP)}</p>`;
}

export function inviteMarkup(invite, username) {
  if (!invite) return '';
  const link = `${globalThis.location?.origin || ''}/${invite.path}`;
  return `<div class="notice invite-box" role="status"><p><b>Invite link for ${esc(username)}</b>: send it to them (it works once, for 24 hours; it is not shown again).</p><div class="form-row"><input class="invite-link" readonly value="${escValue(link)}" aria-label="Invite link"><button type="button" data-copy-invite>Copy</button></div></div>`;
}

function userRow(user, projects, me) {
  const state = user.disabled ? '<span class="pill bad">disabled</span>' : user.invited ? '<span class="pill warn">invited</span>' : '';
  const you = user.name === me ? ' <span class="subtle">(you)</span>' : '';
  return `<li class="user-row" data-user="${escValue(user.name)}"><div class="user-main"><b>${esc(user.username)}</b>${you} <span class="chip">${user.role === 'admin' ? 'Administrator' : 'Member'}</span> ${state}<div class="subtle">${esc(accessSummary(user, projects))} · last seen ${esc(when(user.last_seen))}</div></div><div class="user-actions"><button type="button" data-user-edit="${escValue(user.name)}">Change</button><button type="button" data-user-invite="${escValue(user.name)}">${user.invited ? 'New invite link' : 'Reset sign-in'}</button>${
    user.name === me ? '' : `<button type="button" data-user-disable="${escValue(user.name)}" data-disabled="${user.disabled ? '1' : ''}">${user.disabled ? 'Enable' : 'Disable'}</button><button type="button" class="danger-button" data-user-remove="${escValue(user.name)}">Remove</button>`
  }</div></li>`;
}

export function editMarkup(user, projects) {
  return `<form class="settings-card user-edit" data-user-form="${escValue(user.name)}"><h3>${esc(user.username)}</h3><label class="field">Role<select name="role"><option value="member"${user.role !== 'admin' ? ' selected' : ''}>Member: only the projects below</option><option value="admin"${user.role === 'admin' ? ' selected' : ''}>Administrator: everything</option></select></label>${accessFields(projects, user.access, 'access', user.role === 'admin')}<div class="settings-actions"><button type="submit" class="primary">Save</button><button type="button" data-user-cancel>Cancel</button><span class="form-status" role="status"></span></div></form>`;
}

export function usersMarkup(data, projects = [], me = '', invite = null) {
  const users = data?.users || [];
  return `<div class="settings-card"><h3>People</h3>${invite ? inviteMarkup(invite.invite, invite.username) : ''}<ul class="user-list">${users.map(user => userRow(user, projects, me)).join('')}</ul><div id="user-edit-slot"></div></div><form id="user-add-form" class="settings-card" autocomplete="off"><h3>Add someone</h3><label class="field">Username<input name="username" required pattern="[A-Za-z0-9._-]{2,40}" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="e.g. sam"></label><label class="field">Role<select name="role"><option value="member">Member: only the projects below</option><option value="admin">Administrator: everything</option></select></label>${accessFields(projects)}<div class="settings-actions"><button type="submit" class="primary">Add and get an invite link</button><span class="form-status" role="status"></span></div><p class="subtle">Everyone uses this server's AI accounts (Settings → AI); nobody needs their own.</p></form>`;
}

// {project: level} from a form's access selects (empty = no access).
export function grantsFrom(form, prefix = 'access') {
  const grants = {};
  for (const [key, value] of new FormData(form).entries()) {
    if (key.startsWith(`${prefix}:`) && value) grants[key.slice(prefix.length + 1)] = value;
  }
  return grants;
}

let cache = { users: null, projects: [], me: '', invite: null };

export async function usersData() {
  const [users, projects, me] = await Promise.all([
    requestJSON('/api/users'),
    requestJSON('/api/projects').catch(() => []),
    requestJSON('/api/me').catch(() => ({}))
  ]);
  cache = { ...cache, users, projects: Array.isArray(projects) ? projects : [], me: me.name || '' };
  const html = usersMarkup(users, cache.projects, cache.me, cache.invite);
  cache.invite = null; // shown once
  return html;
}

if (typeof document !== 'undefined') {
  const redraw = () => window.dispatchEvent(new CustomEvent('laika:settings-redraw'));
  const say = (form, message) => {
    const node = form?.querySelector('.form-status');
    if (node) node.textContent = message;
  };
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (form?.id === 'user-add-form') {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(form).entries());
      try {
        const made = await requestJSON('/api/users', { method: 'POST', body: JSON.stringify({ username: values.username.trim(), role: values.role, access: values.role === 'admin' ? {} : grantsFrom(form) }) });
        cache.invite = { invite: made.invite, username: made.user.username };
        redraw();
      } catch (error) {
        say(form, error.message);
      }
    } else if (form?.dataset?.userForm) {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(form).entries());
      try {
        await requestJSON(`/api/users/${encodeURIComponent(form.dataset.userForm)}`, { method: 'PATCH', body: JSON.stringify({ role: values.role, access: values.role === 'admin' ? {} : grantsFrom(form) }) });
        redraw();
      } catch (error) {
        say(form, error.message);
      }
    }
  });
  // Administrators see everything: no project list for them.
  document.addEventListener('change', event => {
    if (event.target?.name !== 'role') return;
    const form = event.target.closest('#user-add-form, [data-user-form]');
    const list = form?.querySelector('[data-member-only]');
    if (list) list.hidden = event.target.value === 'admin';
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button || !button.closest('.settings-body')) return;
    const users = cache.users?.users || [];
    try {
      if (button.dataset.userEdit) {
        const user = users.find(item => item.name === button.dataset.userEdit);
        const slot = document.getElementById('user-edit-slot');
        if (user && slot) slot.innerHTML = editMarkup(user, cache.projects);
      } else if (button.dataset.userCancel !== undefined) {
        const slot = document.getElementById('user-edit-slot');
        if (slot) slot.innerHTML = '';
      } else if (button.dataset.userInvite) {
        if (!window.confirm('Make a new invite link? Their current password stops working and they are signed out everywhere.')) return;
        const made = await requestJSON(`/api/users/${encodeURIComponent(button.dataset.userInvite)}/invite`, { method: 'POST' });
        cache.invite = { invite: made.invite, username: made.user.username };
        redraw();
      } else if (button.dataset.userDisable) {
        const enable = button.dataset.disabled === '1';
        await requestJSON(`/api/users/${encodeURIComponent(button.dataset.userDisable)}`, { method: 'PATCH', body: JSON.stringify({ disabled: !enable }) });
        redraw();
      } else if (button.dataset.userRemove) {
        if (!window.confirm('Remove this person? Their sign-ins and paired phones stop working.')) return;
        await requestJSON(`/api/users/${encodeURIComponent(button.dataset.userRemove)}`, { method: 'DELETE' });
        redraw();
      } else if (button.dataset.copyInvite !== undefined) {
        const input = button.parentElement.querySelector('.invite-link');
        input.select();
        try {
          await navigator.clipboard.writeText(input.value);
          button.textContent = 'Copied';
        } catch {
          document.execCommand?.('copy');
        }
      }
    } catch (error) {
      window.alert(error.message);
    }
  });
}
