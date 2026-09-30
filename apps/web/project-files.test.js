import test from 'node:test';
import assert from 'node:assert/strict';
import { formatSize, downloadUrl, breadcrumbMarkup, listingMarkup, filesPageMarkup, joinPath } from './lib/project-files.js';

test('sizes, paths and download links', () => {
  assert.equal(formatSize(512), '512 B');
  assert.equal(formatSize(1536), '1.5 KB');
  assert.equal(formatSize(null), '');
  assert.equal(joinPath('', 'a'), 'a');
  assert.equal(joinPath('src', 'a.js'), 'src/a.js');
  assert.equal(downloadUrl('shop', 'code', 'src/a b.js'), '/api/projects/shop/files/download?area=code&path=src%2Fa+b.js');
});

test('breadcrumb navigates to every parent', () => {
  const html = breadcrumbMarkup('data', 'img/icons');
  assert.match(html, /data-file-nav="">App data/);
  assert.match(html, /data-file-nav="img">img/);
  assert.match(html, /data-file-nav="img\/icons">icons/);
});

test('listing: folders navigate, files download, only data can be deleted, names escaped', () => {
  const entries = [
    { name: 'src', type: 'dir', size: null, modified: 1 },
    { name: '<x>.js', type: 'file', size: 10, modified: 1 },
    { name: 'link', type: 'link', size: null, modified: 1 }
  ];
  const code = listingMarkup('shop', 'code', '', entries);
  assert.match(code, /data-file-nav="src">src\//);
  assert.match(code, /href="\/api\/projects\/shop\/files\/download\?area=code&amp;path=%3Cx%3E.js" download>&lt;x&gt;.js/);
  assert.doesNotMatch(code, /data-file-delete/);
  assert.match(code, /link \(link\)/);
  const data = listingMarkup('shop', 'data', 'img', entries);
  assert.match(data, /data-file-delete="img\/src"/);
  assert.equal(listingMarkup('shop', 'data', '', []), '<div class="empty">This folder is empty</div>');
});

test('page: tabs, upload input, new folder only for data, code upload warning', () => {
  const code = filesPageMarkup({ projectId: 'shop', area: 'code', path: '' }, { entries: [] });
  assert.match(code, /class="active" data-file-area="code"/);
  assert.match(code, /id="project-file-input" multiple/);
  assert.doesNotMatch(code, /data-file-new-folder/);
  assert.match(code, /committed straight to main/);
  const data = filesPageMarkup({ projectId: 'shop', area: 'data', path: '' }, { error: 'Nothing here yet' });
  assert.match(data, /data-file-new-folder/);
  assert.match(data, /Nothing here yet/);
});
