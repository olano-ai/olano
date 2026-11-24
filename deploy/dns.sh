#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# dns.sh — point a branded host (<name>.olano.run) at a deployment IP.
#
# Gives MANUAL deploys (DigitalOcean, AWS, Tencent — any cloud) the same
# customer-facing hostnames the managed control plane creates for its droplets
# (cloud repo, olano_worker/runner.py:_ensure_slug_dns): an A record on the
# DigitalOcean-hosted zone, upserted idempotently, TTL 180 so re-pointing a
# deployment to a new VM propagates in ~3 minutes. The VM's cloud is
# irrelevant — only the ZONE lives on DigitalOcean DNS.
#
# Usage:
#   deploy/dns.sh ensure acme.olano.run 203.0.113.10   # create or update
#   deploy/dns.sh show   acme.olano.run                # print current record(s)
#   deploy/dns.sh delete acme.olano.run                # remove the A record(s)
#
# Auth — one of:
#   DIGITALOCEAN_TOKEN=dop_v1_…   read+write API token (uses curl + python3)
#   doctl                          authenticated CLI (doctl auth init)
#
# Env:
#   OLANO_DNS_DOMAIN   zone to edit (default: olano.run)
#
# Never run this ON a customer VM — zone credentials stay on your laptop / in
# the control plane. Customer boxes only ever hold their own TLS key.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

ZONE="${OLANO_DNS_DOMAIN:-olano.run}"
TTL=180   # matches the managed worker's DNSRecordSpec default
API="https://api.digitalocean.com/v2"

usage() { sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1; }

CMD="${1:-}"; HOST="${2:-}"; IP="${3:-}"
case "$CMD" in ensure|show|delete) ;; *) usage ;; esac
[ -n "$HOST" ] || usage

# The record name is the host minus the zone suffix.
case "$HOST" in
  "$ZONE") die "Host must be a subdomain of ${ZONE} (e.g. acme.${ZONE})." ;;
  .*|*..*) die "Invalid host '${HOST}'." ;;
  *".${ZONE}") SUB="${HOST%".${ZONE}"}" ;;
  *) die "Host '${HOST}' is not under the zone '${ZONE}' (override with OLANO_DNS_DOMAIN)." ;;
esac
if [ "$CMD" = "ensure" ]; then
  [ -n "$IP" ] || die "ensure needs an IP: deploy/dns.sh ensure ${HOST} <public-ip>"
fi

# ── backend selection: explicit token first, else authenticated doctl ─────────
BACKEND=""
if [ -n "${DIGITALOCEAN_TOKEN:-}" ]; then
  command -v python3 >/dev/null 2>&1 || die "python3 required to parse API responses."
  BACKEND=api
elif command -v doctl >/dev/null 2>&1; then
  BACKEND=doctl
else
  die "Set DIGITALOCEAN_TOKEN=dop_v1_… (read+write) or install+auth doctl. The ${ZONE} zone is on DigitalOcean DNS."
fi

# ── list matching A records → lines of "<id>\t<data>" ─────────────────────────
list_records() {
  if [ "$BACKEND" = doctl ]; then
    doctl compute domain records list "$ZONE" --format ID,Type,Name,Data --no-header \
      | awk -v sub_name="$SUB" '$2=="A" && $3==sub_name {print $1 "\t" $4}'
  else
    curl -fsS -H "Authorization: Bearer ${DIGITALOCEAN_TOKEN}" \
      "${API}/domains/${ZONE}/records?type=A&name=${HOST}&per_page=200" \
      | python3 -c '
import json, sys
sub = sys.argv[1]
for r in json.load(sys.stdin).get("domain_records", []):
    if r.get("type") == "A" and r.get("name") == sub:
        print("%s\t%s" % (r["id"], r["data"]))' "$SUB"
  fi
}

create_record() {
  if [ "$BACKEND" = doctl ]; then
    doctl compute domain records create "$ZONE" \
      --record-type A --record-name "$SUB" --record-data "$IP" --record-ttl "$TTL" >/dev/null
  else
    curl -fsS -X POST -H "Authorization: Bearer ${DIGITALOCEAN_TOKEN}" -H "Content-Type: application/json" \
      -d "{\"type\":\"A\",\"name\":\"${SUB}\",\"data\":\"${IP}\",\"ttl\":${TTL}}" \
      "${API}/domains/${ZONE}/records" >/dev/null
  fi
}

update_record() { # $1 = record id
  if [ "$BACKEND" = doctl ]; then
    doctl compute domain records update "$ZONE" --record-id "$1" \
      --record-type A --record-name "$SUB" --record-data "$IP" --record-ttl "$TTL" >/dev/null
  else
    curl -fsS -X PUT -H "Authorization: Bearer ${DIGITALOCEAN_TOKEN}" -H "Content-Type: application/json" \
      -d "{\"type\":\"A\",\"name\":\"${SUB}\",\"data\":\"${IP}\",\"ttl\":${TTL}}" \
      "${API}/domains/${ZONE}/records/$1" >/dev/null
  fi
}

delete_record() { # $1 = record id
  if [ "$BACKEND" = doctl ]; then
    doctl compute domain records delete "$ZONE" "$1" --force
  else
    curl -fsS -X DELETE -H "Authorization: Bearer ${DIGITALOCEAN_TOKEN}" \
      "${API}/domains/${ZONE}/records/$1"
  fi
}

RECORDS="$(list_records || true)"

case "$CMD" in
  show)
    if [ -z "$RECORDS" ]; then
      say "No A record for ${HOST} on ${ZONE}."
    else
      printf '%s\n' "$RECORDS" | while IFS=$'\t' read -r id data; do
        ok "${HOST} → ${data}  (record ${id}, ttl ${TTL})"
      done
    fi
    ;;
  ensure)
    if [ -z "$RECORDS" ]; then
      create_record
      ok "Created ${HOST} → ${IP} (ttl ${TTL})"
    else
      FIRST_ID="$(printf '%s\n' "$RECORDS" | head -n1 | cut -f1)"
      FIRST_DATA="$(printf '%s\n' "$RECORDS" | head -n1 | cut -f2)"
      if [ "$FIRST_DATA" = "$IP" ]; then
        ok "${HOST} already → ${IP} (record ${FIRST_ID})"
      else
        update_record "$FIRST_ID"
        ok "Updated ${HOST}: ${FIRST_DATA} → ${IP} (record ${FIRST_ID})"
      fi
      # Upsert-not-duplicate, same as the managed worker: stray extra copies
      # (e.g. from a hand-created record) are removed so exactly one remains.
      printf '%s\n' "$RECORDS" | tail -n +2 | while IFS=$'\t' read -r id data; do
        [ -n "$id" ] || continue
        delete_record "$id"
        say "Removed duplicate A record ${id} (${data})"
      done
    fi
    ;;
  delete)
    if [ -z "$RECORDS" ]; then
      say "Nothing to delete — no A record for ${HOST}."
    else
      printf '%s\n' "$RECORDS" | while IFS=$'\t' read -r id data; do
        delete_record "$id"
        ok "Deleted ${HOST} → ${data} (record ${id})"
      done
    fi
    ;;
esac
