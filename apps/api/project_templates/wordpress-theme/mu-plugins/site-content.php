<?php
/**
 * Plugin Name: Site content
 * Description: Puts the pages, menu, images and site settings written in the site's project (content/) into WordPress after each deploy. A page someone changed in WordPress since is kept as they left it (shown in Pages → "Kept: changed in WordPress").
 */

const SITE_CONTENT_DIR = '/opt/site-content';

function site_content_files($dir, $pattern) {
    $files = glob(SITE_CONTENT_DIR . '/' . $dir . '/' . $pattern) ?: [];
    sort($files);
    return $files;
}

function site_content_version() {
    $hash = '';
    foreach (array_merge(site_content_files('pages', '*.html'), site_content_files('media', '*'), [SITE_CONTENT_DIR . '/site.json']) as $file) {
        if (is_file($file)) {
            $hash .= basename($file) . sha1_file($file);
        }
    }
    return sha1($hash);
}

/** "title: Home" lines in the leading comment of a page file, and the block markup after it. */
function site_content_page($file) {
    $text = file_get_contents($file);
    $meta = [];
    if (preg_match('/^<!--(.*?)-->\s*/s', $text, $m)) {
        foreach (preg_split('/\R/', trim($m[1])) as $line) {
            if (preg_match('/^\s*([a-z_]+)\s*:\s*(.*?)\s*$/', $line, $kv)) {
                $meta[$kv[1]] = $kv[2];
            }
        }
        $text = substr($text, strlen($m[0]));
    }
    return [$meta, $text];
}

function site_content_media() {
    $urls = [];
    require_once ABSPATH . 'wp-admin/includes/image.php';
    foreach (site_content_files('media', '*') as $file) {
        $name = basename($file);
        if ($name[0] === '.' || !is_file($file)) {
            continue;
        }
        $hash = sha1_file($file);
        $found = get_posts(['post_type' => 'attachment', 'post_status' => 'any', 'numberposts' => 1,
                            'meta_key' => '_site_source', 'meta_value' => $name]);
        if ($found && get_post_meta($found[0]->ID, '_site_hash', true) === $hash) {
            $urls[$name] = wp_get_attachment_url($found[0]->ID);
            continue;
        }
        $upload = wp_upload_bits($name, null, file_get_contents($file));
        if (!empty($upload['error'])) {
            continue;
        }
        $type = wp_check_filetype($upload['file']);
        $id = wp_insert_attachment(['post_title' => pathinfo($name, PATHINFO_FILENAME), 'post_mime_type' => $type['type'] ?: 'application/octet-stream',
                                    'post_status' => 'inherit'], $upload['file']);
        wp_update_attachment_metadata($id, wp_generate_attachment_metadata($id, $upload['file']));
        update_post_meta($id, '_site_source', $name);
        update_post_meta($id, '_site_hash', $hash);
        if ($found) {
            wp_delete_attachment($found[0]->ID, true);
        }
        $urls[$name] = wp_get_attachment_url($id);
    }
    return $urls;
}

function site_content_import() {
    $media = site_content_media();
    $pages = [];
    $kept = [];
    foreach (site_content_files('pages', '*.html') as $file) {
        $slug = basename($file, '.html');
        [$meta, $body] = site_content_page($file);
        $body = preg_replace_callback('/\{\{media:([A-Za-z0-9._-]+)\}\}/', function ($m) use ($media) {
            return esc_url($media[$m[1]] ?? '');
        }, $body);
        $existing = get_page_by_path($slug, OBJECT, 'page');
        $data = ['post_type' => 'page', 'post_status' => 'publish', 'post_name' => $slug,
                 'post_title' => $meta['title'] ?? ucfirst($slug), 'post_content' => $body,
                 'menu_order' => (int) ($meta['order'] ?? 0)];
        if ($existing) {
            $imported = get_post_meta($existing->ID, '_site_imported', true);
            if ($imported && $imported !== sha1($existing->post_content)) {
                $kept[] = $slug;  // changed in WordPress since the last deploy: keep the person's version
                $pages[$slug] = $existing->ID;
                continue;
            }
            $data['ID'] = $existing->ID;
        }
        $id = wp_insert_post(wp_slash($data), true);
        if (is_wp_error($id)) {
            continue;
        }
        update_post_meta($id, '_site_imported', sha1(get_post($id)->post_content));
        $pages[$slug] = $id;
    }
    $site = json_decode((string) @file_get_contents(SITE_CONTENT_DIR . '/site.json'), true) ?: [];
    if (!empty($site['title'])) {
        update_option('blogname', $site['title']);
    }
    if (isset($site['tagline'])) {
        update_option('blogdescription', $site['tagline']);
    }
    if (!empty($site['front_page']) && isset($pages[$site['front_page']])) {
        update_option('show_on_front', 'page');
        update_option('page_on_front', $pages[$site['front_page']]);
    }
    if (!empty($site['posts_page']) && isset($pages[$site['posts_page']])) {
        update_option('page_for_posts', $pages[$site['posts_page']]);
    }
    if (!empty($site['menu'])) {
        $links = '';
        foreach ($site['menu'] as $slug) {
            if (isset($pages[$slug])) {
                $links .= sprintf('<!-- wp:navigation-link {"label":%s,"type":"page","id":%d,"url":%s,"kind":"post-type"} /-->',
                    wp_json_encode(get_the_title($pages[$slug])), $pages[$slug], wp_json_encode(get_permalink($pages[$slug])));
            }
        }
        $menu = get_posts(['post_type' => 'wp_navigation', 'post_status' => 'publish', 'numberposts' => 1,
                           'meta_key' => '_site_menu', 'meta_value' => '1']);
        $id = wp_insert_post(wp_slash(['ID' => $menu ? $menu[0]->ID : 0, 'post_type' => 'wp_navigation',
                                       'post_status' => 'publish', 'post_title' => 'Site menu', 'post_content' => $links]));
        if (!is_wp_error($id)) {
            update_post_meta($id, '_site_menu', '1');
        }
    }
    update_option('site_content_kept', $kept);
}

add_action('init', function () {
    if (!is_dir(SITE_CONTENT_DIR) || !get_option('siteurl') || wp_installing()) {
        return;
    }
    $version = site_content_version();
    if (get_option('site_content_version') === $version) {
        return;
    }
    if (get_transient('site_content_running')) {
        return;
    }
    set_transient('site_content_running', 1, 120);
    site_content_import();
    update_option('site_content_version', $version);
    delete_transient('site_content_running');
}, 20);

add_action('admin_notices', function () {
    $kept = get_option('site_content_kept');
    if ($kept && current_user_can('edit_pages')) {
        printf('<div class="notice notice-info"><p>Kept: changed in WordPress, so the site project\'s newer version was not applied: %s</p></div>',
               esc_html(implode(', ', $kept)));
    }
});
