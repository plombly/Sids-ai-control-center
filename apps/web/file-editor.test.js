import test from 'node:test';
import assert from 'node:assert/strict';
import { caretPosition, editorMarkup, indentOf, isEditable } from './lib/file-editor.js';
import { menuItems } from './lib/project-files.js';

test('only text files open in the editor', () => {
  for (const name of ['app.js', 'README.md', '.env', 'Dockerfile', 'config.yaml', 'main.py', 'notes'])
    assert.ok(isEditable({ name, type: 'file', size: 100 }), name);
  for (const name of ['logo.png', 'a.zip', 'clip.mp4', 'data.sqlite', 'doc.pdf', 'font.woff2'])
    assert.ok(!isEditable({ name, type: 'file', size: 100 }), name);
  assert.ok(!isEditable({ name: 'big.log', type: 'file', size: 3 * 1024 * 1024 }));
  assert.ok(!isEditable({ name: 'src', type: 'dir' }) && !isEditable({ name: 'l', type: 'link' }) && !isEditable(null));
  const keys = items => items.filter(item => item !== '-').map(([key]) => key);
  assert.deepEqual(keys(menuItems({ entries: [{ name: 'a.js', type: 'file', size: 9 }] })).slice(0, 2), ['edit', 'download']);
  assert.ok(!keys(menuItems({ entries: [{ name: 'a.png', type: 'file', size: 9 }] })).includes('edit'));
});

test('indentation follows the file; caret positions are 1-based', () => {
  assert.equal(indentOf('a\n\tb'), '\t');
  assert.equal(indentOf('def f():\n    return 1\n'), '    ');
  assert.equal(indentOf('{\n  "a": 1\n}'), '  ');
  assert.equal(indentOf(''), '  ');
  assert.deepEqual(caretPosition('ab\ncd', 4), { line: 2, column: 2 });
  assert.deepEqual(caretPosition('', 0), { line: 1, column: 1 });
});

test('editor: save only when writable, text escaped, notes per area', () => {
  const html = editorMarkup({ path: 'src/<x>.js', area: 'code', text: 'a\nb' });
  assert.match(html, /data-editor="save"/);
  assert.match(html, /commits it to main/);
  assert.match(html, /src\/&lt;x&gt;\.js/);
  assert.match(html, /2 lines/);
  const view = editorMarkup({ path: 'a.txt', area: 'data', readOnly: true });
  assert.doesNotMatch(view, /data-editor="save"/);
  assert.match(view, / readonly>/);
  assert.match(view, /View access/);
});
