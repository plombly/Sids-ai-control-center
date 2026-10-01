// What a file is "for", by its extension: drives the file browser's
// colour coding and icons (folders gray, code blue, archives red, ...).
const GROUPS = [
  ['archive', 'Archive', ['zip', 'tar', 'gz', 'tgz', 'bz2', 'xz', '7z', 'rar']],
  ['image', 'Image', ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico', 'bmp', 'avif']],
  ['document', 'Document', ['pdf', 'md', 'txt', 'doc', 'docx', 'csv', 'xls', 'xlsx', 'odt', 'rtf', 'ppt', 'pptx']],
  ['config', 'Config / data', ['json', 'yaml', 'yml', 'toml', 'ini', 'env', 'lock', 'db', 'sqlite', 'sqlite3', 'xml', 'conf', 'cfg']],
  ['script', 'Script / executable', ['sh', 'bash', 'zsh', 'bat', 'cmd', 'ps1', 'exe', 'bin', 'run', 'appimage']],
  ['media', 'Audio / video', ['mp3', 'wav', 'ogg', 'flac', 'm4a', 'mp4', 'mov', 'webm', 'mkv', 'avi']]
];
const BY_EXTENSION = new Map(GROUPS.flatMap(([kind, label, exts]) => exts.map(ext => [ext, { kind, label }])));
// Dotfiles that are configuration whatever their "extension".
const CONFIG_NAMES = new Set(['.env', '.gitignore', '.gitattributes', '.editorconfig', '.npmrc', '.dockerignore', 'dockerfile', 'makefile']);

export function fileKind(entry) {
  if (!entry) return { kind: 'code', label: 'File' };
  if (entry.type === 'dir') return { kind: 'folder', label: 'Folder' };
  if (entry.type === 'link') return { kind: 'link', label: 'Link' };
  const name = String(entry.name || '').toLowerCase();
  if (CONFIG_NAMES.has(name) || name.startsWith('.env.')) return { kind: 'config', label: 'Config / data' };
  const dot = name.lastIndexOf('.');
  const ext = dot > 0 ? name.slice(dot + 1) : '';
  return BY_EXTENSION.get(ext) || { kind: 'code', label: 'Code / file' };
}

// Small inline icons drawn with currentColor (no downloads).
const PAGE = 'M6 2h8l4 4v16H6z';
const ICONS = {
  folder: '<path d="M3 6h7l2 2h9v11H3z" fill="currentColor" opacity=".85"/>',
  archive: `<path d="${PAGE}" fill="currentColor" opacity=".85"/><path d="M11 4h2v2h-2zm0 3h2v2h-2zm0 3h2v2h-2zm-1 3h4v4h-4z" fill="var(--bg)"/>`,
  image: `<path d="${PAGE}" fill="currentColor" opacity=".85"/><path d="M8 18l3-4 2 2 2-3 2 5z" fill="var(--bg)"/>`,
  media: `<path d="${PAGE}" fill="currentColor" opacity=".85"/><path d="M10 11v6l5-3z" fill="var(--bg)"/>`,
  script: `<path d="${PAGE}" fill="currentColor" opacity=".85"/><path d="M8.5 12l2.5 2-2.5 2M12 17h3.5" stroke="var(--bg)" stroke-width="1.5" fill="none"/>`,
  link: '<path d="M9 15l6-6M8 11l-2 2a3 3 0 004 4l2-2M16 13l2-2a3 3 0 00-4-4l-2 2" stroke="currentColor" stroke-width="2" fill="none"/>'
};
const DEFAULT_ICON = `<path d="${PAGE}" fill="currentColor" opacity=".85"/><path d="M14 2v4h4" fill="var(--bg)" opacity=".5"/>`;

export function kindIcon(kind) {
  return `<svg class="file-icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">${ICONS[kind] || DEFAULT_ICON}</svg>`;
}
