FROM nginx:1.27-alpine

COPY index.html app.js styles.css /usr/share/nginx/html/
COPY lib /usr/share/nginx/html/lib
COPY brand /usr/share/nginx/html/brand
COPY nginx.conf /etc/nginx/conf.d/default.conf

HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
