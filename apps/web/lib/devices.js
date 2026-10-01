// Settings → Phones & apps (apps/api/device_routes.py): give a phone its
// own key, shown once as a QR code the SID app scans, and revoke it later.
// A device key can read everything and do the safe actions only; it can
// never approve a merge or change settings.
import { requestJSON } from './api.js';
import { esc, escValue } from './format.js';

export function suggestedUrl(location = globalThis.location) {
  const host = location?.hostname || 'localhost';
  return `http://${host}:8000`;
}

function when(seconds, now = Date.now() / 1000) {
  if (!seconds) return 'never';
  const age = now - seconds;
  if (age < 120) return 'just now';
  if (age < 7200) return `${Math.round(age / 60)} min ago`;
  if (age < 172800) return `${Math.round(age / 3600)} h ago`;
  return `${Math.round(age / 86400)} days ago`;
}

export function devicesMarkup(data = {}, url = suggestedUrl(), now = Date.now() / 1000) {
  const devices = Array.isArray(data.devices) ? data.devices : [];
  const rows = devices
    .map(device => `<li class="device-row"><span><b>${esc(device.name)}</b><span class="subtle"> · added ${esc(when(device.created_at, now))} · last used ${esc(when(device.last_used_at, now))}${device.last_seen_from ? ` from ${esc(device.last_seen_from)}` : ''}</span></span><button type="button" class="danger-button" data-device-revoke="${escValue(device.id)}" data-device-name="${escValue(device.name)}">Revoke</button></li>`)
    .join('');
  return `<h2 class="section-title">Phones &amp; apps</h2><div class="settings-card"><p class="subtle">Each phone gets its own key for the SID app. It can see everything and do the safe things (give goals, answer SID's questions), never approve changes into main or change settings. Revoke a lost phone here, and its VPN certificate on your VPN server too.</p>${rows ? `<ul class="device-list">${rows}</ul>` : '<p class="subtle">No phones yet.</p>'}<form id="device-add-form" class="device-add" autocomplete="off"><label class="field">Phone name<input name="name" maxlength="60" placeholder="e.g. Dylan's iPhone" required></label><label class="field">Address the app uses<input name="url" value="${escValue(url)}" required></label><label class="field">Server name in the app<input name="server_name" maxlength="60" value="${escValue(data.server_name || 'SID')}"></label><div class="settings-actions"><button type="submit">Add a phone</button><span id="device-status" class="form-status" role="status"></span></div></form><div id="device-pairing"></div></div>`;
}

export function pairingMarkup(created) {
  const picture = created.qr_svg ? `<div class="pairing-qr">${created.qr_svg}</div>` : '';
  return `<div class="pairing"><h3>Scan this with the SID app</h3><p class="subtle">Shown once. It holds the key for <b>${esc(created.device?.name || '')}</b>; anyone who scans it can use SID as that phone.</p>${picture}<details><summary>Can't scan? Copy the pairing code</summary><textarea readonly rows="3">${esc(created.pairing_text || '')}</textarea></details><button type="button" data-device-done>Done</button></div>`;
}

if (typeof document !== 'undefined') {
  const section = () => document.getElementById('devices-section');
  const say = message => {
    const node = document.getElementById('device-status');
    if (node) node.textContent = message;
  };
  const load = async () => {
    const node = section();
    if (!node) return;
    try {
      node.innerHTML = devicesMarkup(await requestJSON('/api/devices'));
    } catch (error) {
      node.innerHTML = `<div class="empty">${esc(error.message)}</div>`;
    }
  };
  window.addEventListener('sid:settings-loaded', load);
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (form?.id !== 'device-add-form') return;
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    try {
      const created = await requestJSON('/api/devices', {
        method: 'POST',
        body: JSON.stringify({ name: String(values.name || '').trim(), url: String(values.url || '').trim(), server_name: String(values.server_name || '').trim() })
      });
      form.reset();
      const pairing = document.getElementById('device-pairing');
      if (pairing) pairing.innerHTML = pairingMarkup(created);
    } catch (error) {
      say(error.message);
    }
  });
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button || !section()?.contains(button)) return;
    if (button.dataset.deviceDone !== undefined) await load();
    else if (button.dataset.deviceRevoke) {
      if (!globalThis.confirm?.(`Revoke ${button.dataset.deviceName}? The app on it stops working with SID at once.`)) return;
      try {
        await requestJSON(`/api/devices/${encodeURIComponent(button.dataset.deviceRevoke)}`, { method: 'DELETE' });
        await load();
      } catch (error) {
        say(error.message);
      }
    }
  });
}
