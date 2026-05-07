#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CTID="${CTID:-251}"
HOSTNAME="${HOSTNAME:-prismarr}"
PASSWORD="${PASSWORD:-}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
ROOTFS_STORAGE="${ROOTFS_STORAGE:-local-lvm}"
DISK_SIZE_GB="${DISK_SIZE_GB:-24}"
CORES="${CORES:-4}"
MEMORY_MB="${MEMORY_MB:-4096}"
SWAP_MB="${SWAP_MB:-1024}"
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"
GATEWAY="${GATEWAY:-}"
NAMESERVER="${NAMESERVER:-}"
SEARCHDOMAIN="${SEARCHDOMAIN:-}"
ONBOOT="${ONBOOT:-1}"
UNPRIVILEGED="${UNPRIVILEGED:-0}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
TZ="${TZ:-Asia/Yekaterinburg}"
FORCE_RECREATE="${FORCE_RECREATE:-0}"

DOWNLOADS_HOST_PATH="${DOWNLOADS_HOST_PATH:-}"
MOVIES_HOST_PATH="${MOVIES_HOST_PATH:-}"
TV_HOST_PATH="${TV_HOST_PATH:-}"

LXC_DOWNLOADS_PATH="${LXC_DOWNLOADS_PATH:-/srv/prismarr/downloads}"
LXC_MOVIES_PATH="${LXC_MOVIES_PATH:-/srv/prismarr/media/movies}"
LXC_TV_PATH="${LXC_TV_PATH:-/srv/prismarr/media/tv}"

SSH_PUBLIC_KEYS_FILE="${SSH_PUBLIC_KEYS_FILE:-}"

log() {
  printf '[pve] %s\n' "$1"
}

fail() {
  printf '[pve] %s\n' "$1" >&2
  exit 1
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || fail "Required command not found: $cmd"
}

ensure_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    fail "Run this script as root on the Proxmox host"
  fi
}

random_password() {
  local generated
  generated="$(openssl rand -hex 16)"
  printf '%s' "${generated:0:20}"
}

detect_template() {
  local detected
  detected="$(pveam available --section system | awk '/debian-12-standard_.*amd64/ {print $2}' | sort -V | tail -n1)"
  [[ -n "$detected" ]] || fail "Could not find a Debian 12 LXC template via pveam"
  printf '%s' "$detected"
}

container_exists() {
  pct status "$CTID" >/dev/null 2>&1
}

destroy_existing_container() {
  if ! container_exists; then
    return
  fi

  if [[ "$FORCE_RECREATE" != "1" ]]; then
    fail "CT $CTID already exists. Set FORCE_RECREATE=1 to destroy and recreate it."
  fi

  log "Destroying existing CT $CTID"
  pct shutdown "$CTID" --forceStop 1 >/dev/null 2>&1 || true
  pct destroy "$CTID" --purge 1
}

download_template_if_needed() {
  local template_name="$1"
  if pveam list "$TEMPLATE_STORAGE" | grep -Fq "vztmpl/${template_name}"; then
    log "Template already present: $template_name"
    return
  fi

  log "Downloading template $template_name to $TEMPLATE_STORAGE"
  pveam download "$TEMPLATE_STORAGE" "$template_name"
}

build_net0() {
  local net0="name=eth0,bridge=${BRIDGE},ip=${IP}"
  if [[ "$IP" != "dhcp" && -n "$GATEWAY" ]]; then
    net0="${net0},gw=${GATEWAY}"
  fi
  printf '%s' "$net0"
}

push_stack_files() {
  local temp_dir="$1"

  mkdir -p "$temp_dir"
  cat > "$temp_dir/.env" <<EOF
PUID=${PUID}
PGID=${PGID}
TZ=${TZ}
DOWNLOADS_PATH=${LXC_DOWNLOADS_PATH}
MOVIES_PATH=${LXC_MOVIES_PATH}
TV_PATH=${LXC_TV_PATH}
PRISMARR_PORT=7070
JELLYFIN_PORT=8096
JELLYSEERR_PORT=5055
RADARR_PORT=7878
SONARR_PORT=8989
PROWLARR_PORT=9696
QBIT_WEBUI_PORT=8080
QBIT_TORRENT_PORT=6881
TRUSTED_PROXIES=127.0.0.1,REMOTE_ADDR
EOF

  pct exec "$CTID" -- bash -lc "mkdir -p /root/prismarr-bootstrap"
  pct push "$CTID" "$STACK_DIR/docker-compose.yml" /root/prismarr-bootstrap/docker-compose.yml --perms 0644
  pct push "$CTID" "$STACK_DIR/.env.example" /root/prismarr-bootstrap/.env.example --perms 0644
  pct push "$CTID" "$temp_dir/.env" /root/prismarr-bootstrap/.env --perms 0600
  pct push "$CTID" "$SCRIPT_DIR/bootstrap-inside-lxc.sh" /root/prismarr-bootstrap/bootstrap-inside-lxc.sh --perms 0755
}

attach_mountpoint() {
  local index="$1"
  local host_path="$2"
  local container_path="$3"

  [[ -n "$host_path" ]] || return

  mkdir -p "$host_path"
  pct set "$CTID" "-mp${index}" "$host_path,mp=$container_path"
}

ensure_container_reachable() {
  local attempts=30
  local n=1

  while (( n <= attempts )); do
    if pct exec "$CTID" -- bash -lc "test -f /etc/debian_version" >/dev/null 2>&1; then
      return
    fi
    sleep 2
    n=$((n + 1))
  done

  fail "Container did not become ready in time"
}

main() {
  local net0
  local template_name
  local template_volume
  local temp_dir
  local root_password
  local container_ip

  ensure_root
  require_cmd pct
  require_cmd pveam
  if [[ -z "$PASSWORD" ]]; then
    require_cmd openssl
  fi

  destroy_existing_container

  if [[ -z "$PASSWORD" ]]; then
    PASSWORD="$(random_password)"
  fi
  root_password="$PASSWORD"

  log "Refreshing the Proxmox template catalog"
  pveam update
  template_name="$(detect_template)"
  download_template_if_needed "$template_name"
  template_volume="${TEMPLATE_STORAGE}:vztmpl/${template_name}"
  net0="$(build_net0)"

  log "Creating CT $CTID"
  pct create "$CTID" "$template_volume" \
    --hostname "$HOSTNAME" \
    --ostype debian \
    --cores "$CORES" \
    --memory "$MEMORY_MB" \
    --swap "$SWAP_MB" \
    --rootfs "${ROOTFS_STORAGE}:${DISK_SIZE_GB}" \
    --net0 "$net0" \
    --features "nesting=1,keyctl=1,fuse=1" \
    --onboot "$ONBOOT" \
    --unprivileged "$UNPRIVILEGED" \
    --password "$root_password"

  if [[ -n "$NAMESERVER" ]]; then
    pct set "$CTID" -nameserver "$NAMESERVER"
  fi
  if [[ -n "$SEARCHDOMAIN" ]]; then
    pct set "$CTID" -searchdomain "$SEARCHDOMAIN"
  fi
  if [[ -n "$SSH_PUBLIC_KEYS_FILE" ]]; then
    pct set "$CTID" -ssh-public-keys "$SSH_PUBLIC_KEYS_FILE"
  fi

  attach_mountpoint 0 "$DOWNLOADS_HOST_PATH" "$LXC_DOWNLOADS_PATH"
  attach_mountpoint 1 "$MOVIES_HOST_PATH" "$LXC_MOVIES_PATH"
  attach_mountpoint 2 "$TV_HOST_PATH" "$LXC_TV_PATH"

  log "Starting CT $CTID"
  pct start "$CTID"
  ensure_container_reachable

  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  push_stack_files "$temp_dir"

  log "Installing Docker and starting the Prismarr stack inside the container"
  pct exec "$CTID" -- bash -lc "/root/prismarr-bootstrap/bootstrap-inside-lxc.sh"

  container_ip="$(pct exec "$CTID" -- bash -lc "hostname -I | awk '{print \$1}'" 2>/dev/null || true)"

  printf '\n'
  printf 'Prismarr LXC is ready.\n'
  printf 'CTID: %s\n' "$CTID"
  printf 'Hostname: %s\n' "$HOSTNAME"
  printf 'Root password: %s\n' "$root_password"
  if [[ -n "$container_ip" ]]; then
    printf 'Container IP: %s\n' "$container_ip"
    printf 'Prismarr: http://%s:7070\n' "$container_ip"
    printf 'Jellyfin: http://%s:8096\n' "$container_ip"
    printf 'Jellyseerr: http://%s:5055\n' "$container_ip"
    printf 'Radarr: http://%s:7878\n' "$container_ip"
    printf 'Sonarr: http://%s:8989\n' "$container_ip"
    printf 'Prowlarr: http://%s:9696\n' "$container_ip"
    printf 'qBittorrent: http://%s:8080\n' "$container_ip"
  else
    printf 'Container IP: not detected automatically\n'
  fi
}

main "$@"
