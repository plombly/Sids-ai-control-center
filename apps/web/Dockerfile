FROM nginx:1.27-alpine

COPY index.html app.js styles.css /usr/share/nginx/html/
COPY lib /usr/share/nginx/html/lib
# Rendered to /etc/nginx/conf.d/default.conf at start; only defined env vars
# (SID_OPERATOR_TOKEN) are substituted, nginx's own $variables are untouched.
COPY nginx.conf /etc/nginx/templates/default.conf.template

HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD wget -q -O /dev/null http://127.0.0.1/health || exit 1
