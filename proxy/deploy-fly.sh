#!/usr/bin/env bash
# Deploy jev-proxy to Fly.io.
#
#   ./deploy-fly.sh <app-name> [--domain jev.example.com] [--region iad]
#
# Reads TYPESAFE_API_KEY and PROXY_SECRET from the environment or from ./.env (git-ignored).
# - TYPESAFE_API_KEY empty  -> keyless proxy: every caller sends their own Authorization header.
# - TYPESAFE_API_KEY set    -> a PROXY_SECRET (16+ chars) is required; one is generated and saved
#                              to .env if missing. Callers send it as X-Proxy-Secret.
# Secrets reach Fly over stdin (`fly secrets import`), never on a command line, and are never printed.
set -euo pipefail
cd "$(dirname "$0")"

APP="${1:-}"; shift || true
DOMAIN=""; REGION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$APP" ] || { echo "usage: $0 <app-name> [--domain host] [--region code]" >&2; exit 2; }
command -v fly >/dev/null 2>&1 || { echo "flyctl not found. Install: https://fly.io/docs/flyctl/install/" >&2; exit 1; }
fly auth whoami >/dev/null 2>&1 || { echo "not logged in. Run: fly auth login" >&2; exit 1; }

# --- secrets from env or .env (KEY=VALUE lines only; no shell evaluation) -----------------------
read_env() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//' || true; }
KEY="${TYPESAFE_API_KEY:-$(read_env TYPESAFE_API_KEY)}"
SECRET="${PROXY_SECRET:-$(read_env PROXY_SECRET)}"
if [ -n "$KEY" ] && [ "${#SECRET}" -lt 16 ]; then
  SECRET="$(openssl rand -hex 24)"
  touch .env && chmod 600 .env
  grep -v '^PROXY_SECRET=' .env > .env.tmp || true; mv .env.tmp .env
  printf 'PROXY_SECRET=%s\n' "$SECRET" >> .env
  chmod 600 .env
  echo "generated a PROXY_SECRET and saved it to proxy/.env (git-ignored). Read it from there; it is not printed."
fi

# Optional keyless mode (see README): OPEN_SERVER_KEY=1 [OPEN_RATE_PER_MIN, OPEN_DAILY_MAX] in the environment or .env
OPEN="${OPEN_SERVER_KEY:-$(read_env OPEN_SERVER_KEY)}"
OPEN_RATE="${OPEN_RATE_PER_MIN:-$(read_env OPEN_RATE_PER_MIN)}"
OPEN_DAILY="${OPEN_DAILY_MAX:-$(read_env OPEN_DAILY_MAX)}"

# --- app ---------------------------------------------------------------------------------------
if ! fly apps list 2>/dev/null | awk '{print $1}' | grep -qx "$APP"; then
  fly apps create "$APP"
fi
sed -i.bak -E "s/^app = .*/app = \"$APP\"/" fly.toml && rm -f fly.toml.bak
[ -n "$REGION" ] && { sed -i.bak -E "s/^primary_region = .*/primary_region = \"$REGION\"/" fly.toml && rm -f fly.toml.bak; }

if [ -n "$KEY" ]; then
  {
    printf 'TYPESAFE_API_KEY=%s\nPROXY_SECRET=%s\n' "$KEY" "$SECRET"
    [ "$OPEN" = "1" ] && printf 'OPEN_SERVER_KEY=1\n'
    [ -n "$OPEN_RATE" ] && printf 'OPEN_RATE_PER_MIN=%s\n' "$OPEN_RATE"
    [ -n "$OPEN_DAILY" ] && printf 'OPEN_DAILY_MAX=%s\n' "$OPEN_DAILY"
  } | fly secrets import --app "$APP" --stage
  echo "staged: TYPESAFE_API_KEY, PROXY_SECRET$([ "$OPEN" = "1" ] && echo ', OPEN_SERVER_KEY=1 (callers with no credentials are served with your key, rate- and budget-limited)')"
else
  echo "no TYPESAFE_API_KEY: deploying keyless (callers send their own key)"
fi

# --- deploy + verify ---------------------------------------------------------------------------
fly deploy --app "$APP" --config fly.toml --ha=false
HOST="$APP.fly.dev"
# Fly's own machine check is the source of truth; a new hostname may not resolve on your network for a few minutes.
for i in 1 2 3 4 5 6 7 8; do
  fly status --app "$APP" 2>/dev/null | grep -q "passing" && { echo "machine healthy (Fly health check passing)"; break; }
  [ "$i" = 8 ] && { echo "health check not passing; see: fly logs --app $APP" >&2; exit 1; }
  sleep 5
done
curl -fsS -m 10 "https://$HOST/healthz" >/dev/null 2>&1 && echo "reachable from here: https://$HOST/healthz" \
  || echo "note: $HOST does not resolve from this machine yet (new names can take a few minutes); check from another network or: dig +short $HOST @1.1.1.1"

if [ -n "$DOMAIN" ]; then
  fly certs add "$DOMAIN" --app "$APP" || true
  echo "DNS: create  CNAME  $DOMAIN  ->  $HOST   (then: fly certs check $DOMAIN --app $APP)"
fi

URL="https://${DOMAIN:-$HOST}/v1/systemone"
echo
echo "endpoint:  $URL"
echo "demo page: https://www.senthilsweb.com/jev-pii/?endpoint=$URL"
[ -n "$KEY" ] && echo "server-key mode: send header  X-Proxy-Secret: <PROXY_SECRET from proxy/.env>"
[ "$OPEN" = "1" ] && echo "open mode: no credentials needed (limits: ${OPEN_RATE:-20}/min per client, ${OPEN_DAILY:-1000}/day total per machine)"
echo "rotate a secret: edit .env and re-run this script. Logs: fly logs --app $APP"
