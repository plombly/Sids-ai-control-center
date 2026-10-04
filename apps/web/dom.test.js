import test from 'node:test';
import assert from 'node:assert/strict';
import { inUse, setHTML } from './lib/dom.js';

// A tiny DOM: nodes know their children; the document knows the selection and focus.
const node = (name, children = []) => ({ name, children, contains(other) { return other === this || this.children.some(child => child.contains?.(other) || child === other); }, matches: sel => sel.includes(name), innerHTML: '' });

test('a panel being read, selected or typed in is not redrawn; the next refresh draws it', () => {
  const input = node('textarea');
  const text = node('#text');
  const panel = node('div', [input, text]);
  const doc = { body: node('body'), activeElement: null, getSelection: () => selection };
  let selection = { isCollapsed: true, rangeCount: 0 };
  globalThis.document = doc;
  assert.equal(setHTML(panel, '<p>1</p>'), true);
  assert.equal(setHTML(panel, '<p>1</p>'), false); // unchanged
  selection = { isCollapsed: false, rangeCount: 1, getRangeAt: () => ({ startContainer: text, endContainer: text }) };
  assert.equal(inUse(panel, doc), true);
  assert.equal(setHTML(panel, '<p>2</p>'), false); // selecting: left alone
  assert.equal(panel.innerHTML, '<p>1</p>');
  selection = { isCollapsed: true, rangeCount: 0 };
  doc.activeElement = input;
  assert.equal(setHTML(panel, '<p>2</p>'), false); // typing: left alone
  assert.equal(setHTML(panel, '<p>2</p>', { force: true }), true); // the person's own action
  doc.activeElement = null;
  assert.equal(setHTML(panel, '<p>3</p>'), true);
  assert.equal(setHTML(null, 'x'), false);
  delete globalThis.document;
});

test('scroll positions marked data-keep-scroll survive a redraw; one at the bottom follows new content', () => {
  const log = { dataset: { keepScroll: 'chat', startAt: 'bottom' }, scrollTop: 0, scrollHeight: 500, clientHeight: 200 };
  const fresh = { dataset: { keepScroll: 'chat', startAt: 'bottom' }, scrollTop: 0, scrollHeight: 700, clientHeight: 200 };
  let current = [log];
  const panel = { set innerHTML(v) { current = [fresh]; }, querySelectorAll: () => current, contains: () => false };
  globalThis.document = { body: {}, activeElement: null, getSelection: () => null };
  log.scrollTop = 300; // at the bottom (500 - 300 - 200 = 0)
  setHTML(panel, 'a', { force: true });
  assert.equal(fresh.scrollTop, 700);
  current = [log]; log.scrollTop = 50; fresh.scrollTop = 0; // scrolled up to read: stays there
  setHTML(panel, 'b', { force: true });
  assert.equal(fresh.scrollTop, 50);
  delete globalThis.document;
});
