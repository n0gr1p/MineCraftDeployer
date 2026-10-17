# MineCraftDeployer

Early **proof of concept** for Minecraft Java Edition dedicated servers on Linux. Includes a web UI, CurseForge URL deployments, server ZIP uploads, world ZIP imports and Docker start/stop/restart/logs.

## Quick start

1. Install Docker Engine and Docker Compose on Linux.
2. Copy `.env.example` to `.env` and generate `MCD_TOKEN` using `openssl rand -hex 32`.
3. Create the absolute `MCD_HOST_DATA` directory (default example: `/srv/minecraft-deployer/data`). Set `MCD_PUBLIC_HOST` to the hostname/address friends will use.
4. For CurseForge URL installs, supply an authorized `CF_API_KEY`.
5. Run `docker compose up -d --build`.
6. Access the portal through an SSH tunnel or separately configured HTTPS reverse proxy. The portal binds **127.0.0.1:8080** by default. Set up Minecraft TCP port forwarding/firewall rules as appropriate.

## MVP limitations

Server-pack ZIPs **must** include a top-level `server.jar` and must already be installed. Forge/Fabric/NeoForge setup scripts and CurseForge *client-export* ZIPs are not supported yet. World ZIPs need exactly one `level.dat`; the whole save folder is imported, but singleplayer-host UUID reconciliation has not been implemented. Initial Minecraft runtime image uses Java 21, so some packs will need Java/version selection later. Player allowlist is enabled; player management is not yet exposed by the portal. No automated backups, deletion, rate limiting, or comprehensive testing.

## Security

**Do not expose this MVP to the public internet or give the token to untrusted users.** The backend mounts the Docker socket, which grants effectively root-level Docker control on the host. The portal should be accessible only over a private/trusted network while developing. Running modpacks themselves means executing third-party Java code in containers, so only use trusted pack sources.

The next major step should replace direct Docker socket use with a least-privilege worker, add per-user authentication, proper resource/port quotas and protected world backups.
