# Prismarr Full Stack for PVE LXC

This stack brings together:

- Prismarr
- Jellyfin
- Jellyseerr
- Prowlarr
- Sonarr
- Radarr
- qBittorrent

This variant is tailored for a Proxmox VE LXC running Docker Compose. `Gluetun` is removed, and `qBittorrent` works as a normal service on the internal Docker bridge.

All app configs are stored in Docker named volumes. Media and downloads are mounted from paths you define in `.env`, so Sonarr and Radarr can see the same files that qBittorrent writes.

## One Command on PVE

If you want the Proxmox host to create the LXC, install Docker inside it, copy this stack, and start everything automatically, use:

```bash
PASSWORD='change-me-now' \
DOWNLOADS_HOST_PATH='/srv/media/downloads' \
MOVIES_HOST_PATH='/srv/media/movies' \
TV_HOST_PATH='/srv/media/tv' \
bash /path/to/stacks/prismarr/pve/create-lxc.sh
```

The host-side bootstrap script is [create-lxc.sh](C:/Users/user/Documents/New%20project/stacks/prismarr/pve/create-lxc.sh). It:

- downloads the latest Debian 12 template via `pveam`
- creates the LXC with `nesting=1`
- mounts your media paths if you provided them
- installs Docker Engine and Compose inside the LXC
- copies the Prismarr stack into `/opt/prismarr-stack`
- runs `docker compose up -d`

Useful environment variables for the one-command install:

- `CTID=251`
- `HOSTNAME=prismarr`
- `ROOTFS_STORAGE=local-lvm`
- `TEMPLATE_STORAGE=local`
- `DISK_SIZE_GB=24`
- `CORES=4`
- `MEMORY_MB=4096`
- `SWAP_MB=1024`
- `BRIDGE=vmbr0`
- `IP=dhcp`
- `GATEWAY=192.168.1.1` for static IP mode
- `UNPRIVILEGED=0` by default for maximum Docker-in-LXC compatibility
- `PUID=1000` and `PGID=1000`
- `TZ=Asia/Yekaterinburg`

## PVE LXC notes

Before starting the stack, the LXC should have:

- Docker and Docker Compose installed
- `features: nesting=1`
- storage mounted into the LXC if you want media outside the container rootfs

Typical PVE flow is:

1. Mount storage from the Proxmox host into the LXC.
2. Set `DOWNLOADS_PATH`, `MOVIES_PATH`, and `TV_PATH` in `.env` to those in-container paths.
3. Start the compose stack inside the LXC.

If you use the one-command bootstrap above, these steps are done for you automatically.

## Start

1. Copy `.env.example` to `.env`.
2. Fill in `DOWNLOADS_PATH`, `MOVIES_PATH`, and `TV_PATH`.
3. Start the stack:

```bash
docker compose --env-file .env up -d
```

Run the command from [stacks/prismarr](C:/Users/user/Documents/New%20project/stacks/prismarr).

## Host URLs

- Prismarr: `http://localhost:7070`
- Jellyfin: `http://localhost:8096`
- Jellyseerr: `http://localhost:5055`
- Radarr: `http://localhost:7878`
- Sonarr: `http://localhost:8989`
- Prowlarr: `http://localhost:9696`
- qBittorrent: `http://localhost:8080`

## Internal URLs

Use these container-to-container URLs in setup screens:

- qBittorrent: `http://qbittorrent:8080`
- Prowlarr: `http://prowlarr:9696`
- Sonarr: `http://sonarr:8989`
- Radarr: `http://radarr:7878`
- Jellyfin: `http://jellyfin:8096`
- Jellyseerr: `http://jellyseerr:5055`
- Prismarr: `http://prismarr:7070`

## Recommended wiring

Set up the stack in this order:

1. Jellyfin: add `/media/movies` and `/media/tv` as libraries.
2. qBittorrent: use `/data/downloads` as the download root.
3. Prowlarr: add your indexers first.
4. Sonarr and Radarr:
   - add qBittorrent as download client at `http://qbittorrent:8080`
   - use `/data/tv` and `/data/movies` as root folders
   - sync indexers from Prowlarr
5. Jellyseerr:
   - media server: `http://jellyfin:8096`
   - Radarr: `http://radarr:7878`
   - Sonarr: `http://sonarr:8989`
6. Prismarr:
   - qBittorrent: `http://qbittorrent:8080`
   - Prowlarr: `http://prowlarr:9696`
   - Sonarr: `http://sonarr:8989`
   - Radarr: `http://radarr:7878`
   - Jellyseerr: `http://jellyseerr:5055`

## Notes

- Prismarr stores secrets in its own internal SQLite volume, which matches the upstream recommendation.
- Jellyseerr uses a named volume for `/app/config`, so its SQLite data stays inside Docker-managed storage.
- For clean imports, keep the same logical paths across services: `/data/downloads`, `/data/movies`, `/data/tv`.
- If the LXC is unprivileged, make sure the mounted media paths are writable by the UID/GID you set as `PUID` and `PGID`.
