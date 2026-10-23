<?php
/**
 * Theme setup. Most of the look lives in theme.json, templates/, parts/ and patterns/.
 */
add_action('after_setup_theme', function () {
    add_theme_support('wp-block-styles');
    add_theme_support('editor-styles');
    add_theme_support('responsive-embeds');
});

add_action('wp_enqueue_scripts', function () {
    $file = get_theme_file_path('assets/site.css');
    if (file_exists($file)) {
        wp_enqueue_style('site', get_theme_file_uri('assets/site.css'), [], (string) filemtime($file));
    }
});
