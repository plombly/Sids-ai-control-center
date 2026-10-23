#!/bin/bash
# Link the theme and the must-use plugin from the image into the site's
# volume, then start WordPress as usual (its own entrypoint installs the
# WordPress files on the first start).
set -euo pipefail
mkdir -p /var/www/html/wp-content/themes /var/www/html/wp-content/mu-plugins
ln -sfn /opt/site-theme /var/www/html/wp-content/themes/site
for plugin in /opt/site-mu-plugins/*.php; do
  [ -e "$plugin" ] && ln -sfn "$plugin" "/var/www/html/wp-content/mu-plugins/$(basename "$plugin")"
done
exec docker-entrypoint.sh "$@"
