// Start / stop a change's preview from its approval card (markup.js
// previewMarkup); the card refreshes with the dashboard's next poll.
import { requestJSON } from './api.js';
import { registerClick } from './registry.js';

const act = (method, label) => async button => {
  const id = button.dataset.previewStart || button.dataset.previewStop;
  button.disabled = true;
  button.textContent = label;
  try {
    await requestJSON(`/api/jobs/${encodeURIComponent(id)}/preview`, { method });
  } catch (error) {
    button.disabled = false;
    button.textContent = error.message;
  }
};

registerClick('previewStart', act('POST', 'Starting…'));
registerClick('previewStop', act('DELETE', 'Stopping…'));
