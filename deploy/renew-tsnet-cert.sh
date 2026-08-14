#!/usr/bin/env bash
#
# Renew the Tailscale-issued TLS certificate uvicorn serves, and restart the
# app only when the certificate actually changed.
#
# Why this exists: `.ts.net` certs are Let's Encrypt certs that Tailscale
# issues, so they last ~90 days and Caddy-style automatic HTTPS does not
# apply. `tailscale cert` is the renewer — it hands back the cached cert
# until renewal is due, then fetches a fresh one — but nothing invokes it on
# a schedule. On 2026-08-12 the cert expired and the dashboard went dark:
# the server stayed perfectly healthy while every browser refused the TLS
# connection, which the PWA presented as "the app loads but there's no data
# and no Etrel control" (the service worker serves the cached shell; /api/*
# is never cached).
#
# Uvicorn reads the cert once at startup, so a fresh file on disk changes
# nothing until the unit restarts. The byte comparison below keeps that
# restart rare: on the ~12 runs out of 13 where Tailscale returns the same
# cert, this script touches nothing and exits.
#
# Install: see docs/raspberry-pi-setup.md §6.5.7.

set -euo pipefail

APP_DIR="${TSNET_CERT_DIR:-/opt/homecenter/HomeEnergyCenter}"
CERT_OWNER="${TSNET_CERT_OWNER:-homecenter:homecenter}"
UNIT="${TSNET_CERT_UNIT:-homeenergycenter.service}"
TAILSCALE="${TAILSCALE_BIN:-/usr/bin/tailscale}"

# systemd captures stdout/stderr into the journal — `journalctl -u tsnet-cert`.
log() { printf '%s\n' "$*"; }

# The cert's CN is the MagicDNS name; derive it the same way §6.5.2 does so
# this script needs no editing when moving to another host.
host="${TSNET_HOST:-}"
if [[ -z "$host" ]]; then
    # `if !` so a failure here reports itself instead of exiting silently
    # under `set -e` — this is the step that breaks when tailscaled is logged
    # out or the node key expired, and it must not fail quietly.
    if ! host="$("$TAILSCALE" status --self --json |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"; then
        log "could not read the tailnet host name from '$TAILSCALE status' — is tailscaled logged in?"
        exit 1
    fi
fi
if [[ -z "$host" ]]; then
    log "tailnet host name came back empty — set TSNET_HOST to override"
    exit 1
fi

crt="$APP_DIR/$host.crt"
key="$APP_DIR/$host.key"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Fails loudly (and systemd marks the unit failed) if the node key expired or
# tailscaled is logged out — the one case where this needs a human.
"$TAILSCALE" cert --cert-file "$tmp/cert" --key-file "$tmp/key" "$host"

expiry() { openssl x509 -in "$1" -noout -enddate 2>/dev/null | cut -d= -f2; }

if [[ -f "$crt" ]] && cmp -s "$tmp/cert" "$crt"; then
    log "cert for $host unchanged (expires $(expiry "$crt")) — no restart needed"
    exit 0
fi

install -m 644 "$tmp/cert" "$crt"
install -m 640 "$tmp/key" "$key"
chown "$CERT_OWNER" "$crt" "$key"

log "installed a new cert for $host (expires $(expiry "$crt")) — restarting $UNIT"
systemctl restart "$UNIT"
