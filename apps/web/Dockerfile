FROM nginx:1.27-alpine

COPY index.html app.js styles.css /usr/share/nginx/html/
COPY lib /usr/share/nginx/html/lib
COPY brand /usr/share/nginx/html/brand
# Rendered to /etc/nginx/conf.d/default.conf at start; only defined env vars
# (LAIKA_OPERATOR_TOKEN) are substituted, nginx's own $variables are untouched.
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --chmod=755 laika-local-addrs.sh /docker-entrypoint.d/15-laika-local-addrs.sh

HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
