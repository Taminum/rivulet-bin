#!/usr/bin/env bash
set -euo pipefail

STACK_SRC_DIR="/root/prismarr-bootstrap"
STACK_DST_DIR="/opt/prismarr-stack"

log() {
  printf '[bootstrap] %s\n' "$1"
}

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    printf 'Missing required file: %s\n' "$path" >&2
    exit 1
  fi
}

require_file "$STACK_SRC_DIR/docker-compose.yml"
require_file "$STACK_SRC_DIR/.env"

export DEBIAN_FRONTEND=noninteractive

log "Installing base packages"
apt-get update
apt-get install -y ca-certificates curl gnupg

log "Removing conflicting Docker packages if present"
apt-get remove -y docker.io docker-doc docker-compose podman-docker containerd runc 2>/dev/null || true

log "Configuring Docker apt repository"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

log "Installing Docker Engine and Compose plugin"
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

install -d "$STACK_DST_DIR"
cp "$STACK_SRC_DIR/docker-compose.yml" "$STACK_DST_DIR/docker-compose.yml"
cp "$STACK_SRC_DIR/.env" "$STACK_DST_DIR/.env"
if [[ -f "$STACK_SRC_DIR/.env.example" ]]; then
  cp "$STACK_SRC_DIR/.env.example" "$STACK_DST_DIR/.env.example"
fi

set -a
source "$STACK_DST_DIR/.env"
set +a

log "Creating media directories"
install -d "$DOWNLOADS_PATH" "$MOVIES_PATH" "$TV_PATH"

log "Pulling and starting the Prismarr stack"
cd "$STACK_DST_DIR"
docker compose --env-file .env pull
docker compose --env-file .env up -d

log "Bootstrap finished"
