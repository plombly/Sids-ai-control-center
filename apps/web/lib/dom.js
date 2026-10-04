// Redrawing without getting in the way. The dashboard refreshes its panels
// every few seconds by replacing their HTML; doing that while someone is
// selecting text, typing, or pressing a button inside a panel wipes the
// selection, the cursor or the click. setHTML() leaves such a panel alone
// (the next refresh draws it) and never touches one whose HTML is unchanged.

let pointerIn = null; // the element a pointer is held down on (selecting, clicking)

if (typeof document !== 'undefined') {
  document.addEventListener('pointerdown', event => (pointerIn = event.target), true);
  const release = () => setTimeout(() => (pointerIn = null), 0); // after the click lands
  document.addEventListener('pointerup', release, true);
  document.addEventListener('pointercancel', release, true);
}

// Is someone using this part of the page right now?
export function inUse(node, doc = globalThis.document) {
  if (!node || !doc) return false;
  const selection = doc.getSelection?.();
  if (selection && !selection.isCollapsed && selection.rangeCount) {
    const range = selection.getRangeAt(0);
    if (node.contains(range.startContainer) || node.contains(range.endContainer)) return true;
  }
  const active = doc.activeElement;
  if (active && active !== doc.body && node.contains(active) && active.matches?.('input, textarea, select, [contenteditable=""], [contenteditable="true"]')) return true;
  return Boolean(pointerIn && node.contains(pointerIn));
}

// Scroll positions inside node that should survive a redraw: elements with
// data-keep-scroll="<key>". One that was scrolled to the bottom stays at the
// bottom (a chat log showing a new message); others keep their position.
function scrollState(node) {
  const kept = new Map();
  node.querySelectorAll?.('[data-keep-scroll]').forEach(element => {
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
    kept.set(element.dataset.keepScroll, { top: element.scrollTop, atBottom });
  });
  return kept;
}

function restoreScroll(node, kept) {
  node.querySelectorAll?.('[data-keep-scroll]').forEach(element => {
    const before = kept.get(element.dataset.keepScroll);
    if (!before) element.scrollTop = element.dataset.startAt === 'bottom' ? element.scrollHeight : 0;
    else element.scrollTop = before.atBottom ? element.scrollHeight : before.top;
  });
}

// node.innerHTML = html, unless nothing changed or the node is in use.
// force: draw anyway (the person's own action asked for it). True if drawn.
export function setHTML(node, html, { force = false } = {}) {
  if (!node) return false;
  if (!force && node.__laikaHtml === html) return false;
  if (!force && inUse(node)) return false;
  const kept = scrollState(node);
  node.innerHTML = html;
  node.__laikaHtml = html;
  restoreScroll(node, kept);
  return true;
}
