#!/usr/bin/env bash
# Install or update Flight Control on a Linux server with Docker.
#
#   GH_TOKEN=<token with repo read> bash install.sh            # site + house airline
#   bash install.sh --public-only                               # site only
#
# Everything lives in ~/flight-control. Re-running pulls and redeploys.
set -euo pipefail
DIR="${FC_DIR:-$HOME/flight-control}"
OWNER=enochlev
auth=""
[ -n "${GH_TOKEN:-}" ] && auth="${GH_TOKEN}@"

command -v docker >/dev/null || { echo "Docker is required"; exit 1; }
docker compose version >/dev/null || { echo "docker compose v2 is required"; exit 1; }

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "https://${auth}github.com/${OWNER}/flight-control.git" "$DIR"
fi
cd "$DIR"

FILES=(-f compose.yaml)
if [ "${1:-}" != "--public-only" ]; then
  if [ -d house/.git ]; then
    git -C house pull --ff-only
  else
    git clone "https://${auth}github.com/${OWNER}/flight-control-house.git" house
  fi
  FILES+=(-f house/compose.house.yaml)
fi

if [ ! -f .env ]; then
  cat > .env <<ENV
FC_ADMIN_PASSWORD=$(openssl rand -hex 12)
FC_HOUSE_KEY=$(openssl rand -hex 16)
FC_PUBLIC_URL=${FC_PUBLIC_URL:-https://flights.enochlev.com}
FC_ALLOWED_HOST_SUFFIXES=trycloudflare.com,enochlev.com
FC_PORT=${FC_PORT:-8000}
ENV
  echo "Wrote $DIR/.env. The admin password is in it (user name admin)."
fi

docker compose "${FILES[@]}" up -d --build
docker compose "${FILES[@]}" ps
echo
echo "Flight Control is on http://127.0.0.1:$(grep FC_PORT .env | cut -d= -f2)"
echo "Point a Cloudflare named tunnel at it, for example:"
echo "  cloudflared tunnel route dns <tunnel> flights.enochlev.com"
echo "  ingress: - hostname: flights.enochlev.com  service: http://127.0.0.1:8000"
