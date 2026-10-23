<?php
/**
 * Plugin Name: Site theme
 * Description: Switches to this site's own theme once, right after WordPress is set up. The owner can choose another theme later.
 */
add_action('init', function () {
    if (get_option('site_theme_chosen')) {
        return;
    }
    if (wp_get_theme('site')->exists() && get_option('stylesheet') !== 'site') {
        switch_theme('site');
    }
    update_option('site_theme_chosen', 1);
});
