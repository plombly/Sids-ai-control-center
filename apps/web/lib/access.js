// What the signed-in person may do (apps/api/access.py). Screens use this to
// show only controls that will work; the server enforces it either way.
// project.my_access: admin | approve | build | view (missing: an older API
// without teams, where everyone signed in is the administrator).
import { requestJSON } from './api.js';

export const RANK = { view: 1, build: 2, approve: 3, admin: 4 };
export const can = (project, level) => (RANK[project?.my_access || 'admin'] || 0) >= RANK[level];

let me = null;
export const currentMe = () => me;
export const isAdmin = () => !me || me.admin !== false;
export function setMe(value, doc = globalThis.document) {
  me = value;
  if (doc?.body) doc.body.dataset.role = isAdmin() ? 'admin' : 'member';
}

export async function loadMe() {
  try {
    setMe(await requestJSON('/api/me'));
  } catch {
    setMe(null);
  }
  globalThis.dispatchEvent?.(new CustomEvent('laika:me', { detail: me }));
  return me;
}
