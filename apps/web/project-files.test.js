import test from 'node:test';
import assert from 'node:assert/strict';
import {
  formatSize,
  joinPath,
  downloadUrl,
  selectClick,
  menuItems,
  canDrop,
  pasteRequest,
  breadcrumbMarkup,
  listingMarkup,
  selectionBarMarkup,
  filesPageMarkup,
  hostConflicts
} from './lib/project-files.js';
import { fileKind, kindIcon } from './lib/file-kinds.js';
import { conflictDialogMarkup, folderPickerMarkup, entryBadge } from './lib/file-dialogs.js';

const names = ['a', 'b', 'c', 'd', 'e'];

test('sizes, paths and downloads (one or many paths)', () => {
  assert.equal(formatSize(1536), '1.5 KB');
  assert.equal(formatSize(null), '');
  assert.equal(joinPath('src', 'a.js'), 'src/a.js');
  assert.equal(downloadUrl('shop', 'code', ['src/a b.js']), '/api/projects/shop/files/download?area=code&path=src%2Fa+b.js');
  assert.equal(downloadUrl('shop', 'data', ['x', 'y']), '/api/projects/shop/files/download?area=data&path=x&path=y');
});

test('selection: click, ctrl+click toggles, shift+click ranges from the anchor', () => {
  let state = selectClick([], names, 1);
  assert.deepEqual(state, { selected: ['b'], anchor: 1 });
  state = selectClick(state.selected, names, 3, { ctrl: true }, state.anchor);
  assert.deepEqual(state.selected, ['b', 'd']);
  state = selectClick(state.selected, names, 1, { ctrl: true }, state.anchor);
  assert.deepEqual(state.selected, ['d']);
  state = selectClick(['b'], names, 3, { shift: true }, 1);
  assert.deepEqual(state, { selected: ['b', 'c', 'd'], anchor: 1 });
  state = selectClick(['b', 'c', 'd'], names, 0, { shift: true }, 1);
  assert.deepEqual(state.selected, ['a', 'b']);
  assert.deepEqual(selectClick(['e'], names, 2, { shift: true, ctrl: true }, 0).selected, ['a', 'b', 'c', 'e']);
});

test('menu: background, single file, folder with clipboard, many, zip and links', () => {
  const keys = items => items.filter(item => item !== '-').map(([key]) => key);
  assert.deepEqual(keys(menuItems({})), ['mkdir', 'upload', 'select-all']);
  const clip = { names: ['x.txt'] };
  assert.equal(menuItems({ clipboard: clip })[0][1], 'Paste x.txt here');
  assert.deepEqual(keys(menuItems({ entries: [{ name: 'a.zip', type: 'file' }] })),
    ['download', 'rename', 'cut', 'copy', 'move-to', 'copy-to', 'zip', 'unzip', 'delete']);
  assert.deepEqual(keys(menuItems({ entries: [{ name: 'src', type: 'dir' }], clipboard: clip })),
    ['open', 'download', 'rename', 'cut', 'copy', 'paste-into', 'move-to', 'copy-to', 'zip', 'delete']);
  const many = menuItems({ entries: [{ name: 'a', type: 'file' }, { name: 'b', type: 'dir' }] });
  assert.ok(!keys(many).includes('rename') && !keys(many).includes('open'));
  assert.ok(many.some(item => item[1] === 'Zip 2 items') && many.some(item => item[1] === 'Delete 2 items'));
  assert.ok(!keys(menuItems({ entries: [{ name: 'l', type: 'link' }] })).includes('zip'));
});

test('drops and pastes never put a folder inside itself; cut pastes move, copies copy', () => {
  assert.equal(canDrop(['src'], 'code', 'src', 'code'), false);
  assert.equal(canDrop(['src'], 'code', 'src/lib', 'code'), false);
  assert.equal(canDrop(['src'], 'code', 'srcs', 'code'), true);
  assert.equal(canDrop(['src'], 'code', 'src', 'data'), true);
  assert.deepEqual(pasteRequest({ mode: 'cut', area: 'data', folder: 'img', names: ['a.png'] }, 'code', 'static'),
    { op: 'move', from_area: 'data', to_area: 'code', paths: ['img/a.png'], dest: 'static' });
  assert.equal(pasteRequest({ mode: 'copy', area: 'code', folder: '', names: ['a'] }, 'code', '').op, 'copy');
  assert.equal(pasteRequest(null, 'code', ''), null);
  assert.deepEqual(hostConflicts('refused: conflict: ["a.txt", "b"]'), ['a.txt', 'b']);
  assert.equal(hostConflicts('disk full'), null);
});

test('listing: rows carry selection, cut, drop targets; names are escaped', () => {
  const html = listingMarkup({
    projectId: 'shop', area: 'code', path: 'site',
    entries: [{ name: 'img', type: 'dir', size: null, modified: 1 }, { name: '<x>.zip', type: 'file', size: 10, modified: 1 }],
    selected: ['img'], clipboard: { projectId: 'shop', area: 'code', folder: 'site', names: ['<x>.zip'], mode: 'cut' }
  });
  assert.match(html, /class="file-row selected" data-name="img" data-type="dir" draggable="true" data-drop-path="site\/img"/);
  assert.match(html, /class="file-row cut" data-name="&lt;x&gt;.zip"/);
  assert.match(html, /kind-archive/);
  assert.match(html, /data-drop-path="site"><table/);
  assert.doesNotMatch(html, /<x>/);
  assert.doesNotMatch(html, /—/);
  assert.match(listingMarkup({ entries: [], path: 'p' }), /data-drop-path="p">This folder is empty/);
  assert.match(selectionBarMarkup(0), /selection-hint/);
  assert.doesNotMatch(selectionBarMarkup(0), /data-file-cmd/);
  assert.match(selectionBarMarkup(3), /3 selected/);
  assert.match(breadcrumbMarkup('data', 'img/icons'), /data-drop-path="img\/icons">icons/);
});

test('page: no per-row dropdowns or checkboxes, tabs and clipboard note', () => {
  const html = filesPageMarkup({ projectId: 'shop', area: 'data', path: '', selected: ['a'], message: '',
    clipboard: { area: 'code', names: ['a', 'b'], mode: 'copy' } }, { entries: [{ name: 'a', type: 'file', size: 1, modified: 1 }] });
  assert.doesNotMatch(html, /<select|type="checkbox"/);
  assert.match(html, /class="active" data-file-area="data"/);
  assert.match(html, /Copied: 2 items \(Code \(main\)\)/);
  assert.match(html, /1 selected/);
});

test('colour coding by use', () => {
  const kind = (name, type = 'file') => fileKind({ name, type }).kind;
  assert.equal(kind('src', 'dir'), 'folder');
  assert.equal(kind('app.js'), 'code');
  assert.equal(kind('README'), 'code');
  assert.equal(kind('site.tar.gz'), 'archive');
  assert.equal(kind('logo.PNG'), 'image');
  assert.equal(kind('notes.md'), 'document');
  assert.equal(kind('package.json'), 'config');
  assert.equal(kind('.env.local'), 'config');
  assert.equal(kind('Dockerfile'), 'config');
  assert.equal(kind('deploy.sh'), 'script');
  assert.equal(kind('intro.mp4'), 'media');
  assert.equal(kind('l', 'link'), 'link');
  assert.match(kindIcon('archive'), /<svg/);
  assert.match(entryBadge({ name: 'a.zip', type: 'file' }), /title="Archive"/);
});

test('conflict dialog and folder picker markup', () => {
  const html = conflictDialogMarkup(['a.txt', '<b>'], 'data');
  assert.match(html, /2 names already exist/);
  assert.match(html, /data-conflict-all/);
  assert.match(html, /<option value="keep" selected>Keep both/);
  assert.match(html, /Overwrite/);
  assert.match(html, /Discard/);
  assert.match(html, /permanent/);
  assert.match(html, /&lt;b&gt;/);
  assert.doesNotMatch(conflictDialogMarkup(['a'], 'code'), /data-conflict-all/);
  const picker = folderPickerMarkup({ area: 'code', path: 'src', folders: ['lib'], areas: ['code', 'data'],
    labels: { code: 'Code (main)', data: 'App data' } });
  assert.match(picker, /data-pick-path="src\/lib"/);
  assert.match(picker, /data-pick-area="data"/);
  assert.match(folderPickerMarkup({ area: 'data', path: '', folders: [], areas: ['data'], labels: { data: 'App data' } }), /No folders here/);
});
