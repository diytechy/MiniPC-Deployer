# Caddy, plus the Cloudflare DNS provider — so certificates need no inbound port.
#
# WHY THIS EXISTS. Until 2026-08-08 the stack ran the stock `caddy:*-alpine`
# image, which can only answer the ACME http-01 and tls-alpn-01 challenges. Both
# require Let's Encrypt to reach this box from the public internet on :80 or
# :443. On a lab hub with the real hub's address, every issuance failed:
#
#   challenge failed  identifier=<domain>  challenge_type=http-01
#   detail="<public ip>: Fetching http://<domain>/.well-known/acme-challenge/…:
#           Connection refused"
#
# and Caddy does not fall back — with no certificate it fails the TLS handshake
# outright (`tlsv1 alert internal error`). So ONE missing router forward took
# down every HTTPS interface at once: the tracker, Actual, the DNS console, the
# apex, and the kiosk site the wall panel exists to display. Eight of the fifteen
# failures in the 2026-08-08 verification run were this single cause.
#
# THE COUPLING IS THE REAL DEFECT, not the router. The kiosk site is LAN-only by
# ratification (OI-12: published on LAN_IP, /32 allow-list, 403 default) and it
# still could not obtain a certificate without a WAN path. A LAN-only service
# whose availability depends on the internet is a design fault regardless of
# whose firewall is in the way.
#
# dns-01 removes the inbound requirement entirely: Caddy proves control of the
# domain by writing a TXT record through the Cloudflare API. Nothing has to reach
# this box. It also works for names that resolve to a private address, which is
# what split-horizon makes every one of these on the LAN.
#
# THE TOKEN IS ALREADY HERE. CLOUDFLARE_API_TOKEN is in .env for the DDNS
# updater and stack/.env.example already specifies it as a scoped DNS-edit
# token — the same Zone:DNS:Edit scope this plugin needs. No new secret, no new
# tier: see SECRET_HANDOFF.md.
#
# BUILT, NOT PULLED, because there is no official image carrying the plugin —
# Caddy's DNS providers are compiled in. scripts/ensure-local-images.sh builds
# it and vmtest/export-images.sh bakes the result into the payload like every
# other image, so the INSTALL still needs no network.

ARG CADDY_VERSION=2.11.4

# ── the plugin build ─────────────────────────────────────────────────────────
# The `-builder` variant is the same Caddy release with xcaddy and a Go
# toolchain. Pinned to the same ARG as the runtime below so the two can never
# drift: a caddy binary built against 2.11.4 running on a 2.12 base is the kind
# of mismatch that surfaces as a config directive silently doing nothing.
FROM caddy:${CADDY_VERSION}-builder AS builder

# Pinned. An unpinned plugin means the image content changes under a tag that
# does not, which breaks the one thing the bake depends on: that what was
# validated is what ships.
#
# v0.2.4, NOT v0.2.1, AND THE REASON IS A LESSON RATHER THAN A VERSION BUMP.
# This shipped as v0.2.1, validated against a made-up token, and passed. On the
# first real box Caddy then refused to load its config AT ALL and crash-looped:
#
#   provision dns.providers.cloudflare: API token '...' appears invalid;
#   ensure it's correctly entered and not wrapped in braces nor quotes
#
# The token was fine. Cloudflare issues User API Tokens with a `cfut_` prefix
# now, and the libdns/cloudflare bundled in v0.2.1 validates a length its own
# provider had outgrown. Measured across both versions with dummy tokens of
# several shapes: v0.2.1 accepts up to 49 characters and rejects 53; v0.2.4
# (libdns/cloudflare v0.2.2) accepts 53. The real token is 53.
#
# WHY THE PRE-SHIP VALIDATION MISSED IT: it was run with a placeholder. That is
# the same trap this project already documents for the identity store - "never
# point HOMEHUB_IDENTITY at the example file, it would report clean no matter
# what real data sat in the tree". A credential-shaped check against a fake
# credential proves the plumbing and nothing about the credential.
#
# Bumping this again is fine; validating it against a REAL token before shipping
# is the part that is not optional. The cost of getting it wrong is not "no
# certificates" - it is no Caddy at all, and every web surface with it.
ARG CLOUDFLARE_PLUGIN_VERSION=v0.2.4

# THE SECOND PLUGIN, added 2026-08-29 (the Owner). basic_auth in front of
# actual.<domain> and dns.<domain> is an ONLINE guessing target: the router
# forwards :80/:443 (HOMELAB_TOPOLOGY.md), Caddy ships no rate limiter in the
# standard build, and the box had no fail2ban. Unlimited attempts against a
# browser prompt is not something bcrypt's cost factor fixes - cost 12 slows an
# OFFLINE crack of a stolen hash, not an online one against a live prompt.
#
# v0.1.0 IS THE ONLY TAG THIS MODULE HAS. `go list -m --versions` returns
# exactly one, so the pin is not a choice between releases - it is the release.
# Said out loud because "pinned" reads like "reviewed the changelog", and here
# there is no changelog to review. The build-time proof below is what makes it
# safe to ship: an incompatible plugin fails the IMAGE, not a renewal at 03:00,
# which is exactly the lesson the CLOUDFLARE_PLUGIN_VERSION comment above
# already paid for once.
#
# BELT AND BRACES BY DESIGN. This throttles; the Caddyfile remote_ip gate on
# those two hosts REMOVES them from the internet; fail2ban bans at the firewall.
# The rate limiter is the one of the three that still helps against a caller
# who is already inside the allow-list.
ARG RATELIMIT_PLUGIN_VERSION=v0.1.0
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare@${CLOUDFLARE_PLUGIN_VERSION} \
    --with github.com/mholt/caddy-ratelimit@${RATELIMIT_PLUGIN_VERSION}

# ── the runtime ──────────────────────────────────────────────────────────────
# Same base the stack ran before, so nothing else about the container changes:
# same paths, same entrypoint, same /data and /config volumes, same user.
FROM caddy:${CADDY_VERSION}-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# Proves the plugin is actually in the binary at BUILD time rather than at 03:00
# on a renewal. `caddy list-modules` names every compiled-in module; without the
# grep this Dockerfile would happily produce a stock Caddy that fails exactly the
# way the stock image already does, and the failure would look like a config
# error in the Caddyfile.
RUN caddy list-modules | grep -q '^dns.providers.cloudflare$' \
    || (echo "FATAL: the cloudflare DNS provider is not in this binary" >&2; exit 1)

# Same proof for the rate limiter, and for the same reason: without it a stock
# Caddy ships, the Caddyfile `rate_limit` block becomes an unknown directive,
# and Caddy refuses to load its config at all - taking every HTTPS surface with
# it. Fail here instead, where it costs a build.
RUN caddy list-modules | grep -q '^http.handlers.rate_limit$' \
    || (echo "FATAL: the rate_limit handler is not in this binary" >&2; exit 1)
