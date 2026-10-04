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
