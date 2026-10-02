# Phones

The LAIka app shows what your server is doing, lets you start goals,
answer the goal assistant, try previews and help stuck jobs, from your
phone.

## Pairing

1. On the dashboard: **Settings → Phones & apps → Pair a phone**. Name it.
2. Scan the QR code with the LAIka app. The code contains the server's
   address and a key for that phone only; it is shown once.
3. Lost the phone? **Revoke** it on the same page; its key stops working at
   once.

A phone can submit goals, use the goal assistant, start builds and
previews, and answer stuck jobs (try again, give up, internet access). It
can **not** approve changes, change settings or pair other devices: those
stay on the dashboard.

## Away from home: use your VPN

LAIka must never be reachable from the internet, so a phone reaches it
through your VPN. The app has OpenVPN built in: import your `.ovpn`
profile once per server, and the app connects through it before talking to
LAIka. Any other VPN on the phone (WireGuard, Tailscale…) works too.

The address in the pairing code is the one you opened the dashboard with.
If the phone reaches the server under a different address through the VPN,
change it in the app.
