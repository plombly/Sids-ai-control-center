# {{NAME}}

A WordPress site, built entirely here: its **theme** (how it looks) and its
**content** (pages, menu, images, title). Both are put into WordPress on
every deploy. People can still edit in WordPress: a page someone changed
there is kept as they left it (WordPress shows which), and blog posts
written in WordPress are never touched.

- `theme/`: a block theme. `theme.json` sets colours, fonts and spacing;
  `templates/` are page layouts, `parts/` the header and footer, `patterns/`
  ready-made sections (PHP files with a pattern header) the owner can insert.
  Use WordPress core blocks only; no page builders, no external CDNs.
- `content/pages/<address>.html`: one page each, block markup after a
  header comment (`title:`, `order:`). The file name is the page's address
  (`about.html` → `/about/`). Images go in `content/media/` and are used as
  `{{media:photo.jpg}}` (e.g. in an image block's `src`).
- `content/site.json`: site title, tagline, `front_page`, optional
  `posts_page` (a blog), and `menu` (page addresses in order).
- `mu-plugins/`: switches the site to this theme once, and imports `content/`
  after each deploy.
- `Dockerfile` + `lnmt-entrypoint.sh`: how LNMT runs it (WordPress image with
  this theme linked in). Every approved change is pushed to LNMT, which
  builds and redeploys; content and uploads are never touched.
- `tests/`: checks run before every change is accepted: valid theme header,
  `theme.json`, balanced block markup in templates, parts and patterns, PHP
  files that open with `<?php`, and the Docker files. Add tests for new rules.

The theme directory is always named `site` inside WordPress.
