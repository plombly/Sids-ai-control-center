#!/bin/sh
# Runs before nginx starts (docker-entrypoint.d). The container shares the
# host's network, so these are every address of this server: requests from
# any of them come from the server itself and get no operator token.
ip -o addr show | awk '{split($4, a, "/"); if (a[1] !~ /^(127\.|::1$)/) print "    " a[1] " 1;"}' > /etc/nginx/sid-local-addrs.conf
echo "sid: $(wc -l < /etc/nginx/sid-local-addrs.conf) local addresses get no operator token"
