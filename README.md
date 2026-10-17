# MineCraftDeployer

Early **proof of concept** for Minecraft Java Edition dedicated servers on Linux. Includes a web UI, CurseForge URL deployments, server ZIP uploads, world ZIP imports and Docker start/stop/restart/logs.

## Intended network architecture: LAN-only portal, public gameplay ports

The administrative website is **LAN-only**. Your daughter accesses it from her browser on your home network. Friends connect to the individual Minecraft game-server ports from outside your network.

| Service | Listener | Router forwarding |
|---|---|---|
| Portal | `MCD_PORTAL_BIND_IP:8080` (LAN interface only) | **Never** forward TCP/8080 |
| Minecraft server 1 | `MCD_PUBLIC_HOST:25565` (TCP) | Forward TCP/25565 if needed |
| Minecraft server 2 | `MCD_PUBLIC_HOST:25566` (TCP) | Forward TCP/25566 if needed |
| Additional servers | TCP/25567–25600 (allocated as needed) | Forward only game ports in use |

This does not require a public domain, internet-facing reverse proxy, or public authentication service for the portal. Router/firewall rules should prevent internet access to the portal. The Docker game server is published on the host's network interfaces independently of the portal binding.

## Quick start

1. Install Docker Engine and Docker Compose on Linux.
2. Copy `.env.example` to `.env` and generate `MCD_TOKEN` using `openssl rand -hex 32`.
3. Set `MCD_PORTAL_BIND_IP` to **the Linux server's LAN IPv4 address** (for example `192.168.1.50`), not `0.0.0.0` or a public address. If you omit it, the portal is loopback-only and remote LAN browsers will not be able to reach it.
4. Create the absolute `MCD_HOST_DATA` directory (example `/srv/minecraft-deployer/data`). Set `MCD_PUBLIC_HOST` to the public DNS name or IP your daughter's friends will use to connect to the **game**. For CurseForge URL installs, supply an authorized `CF_API_KEY`.
5. Run `docker compose up -d --build`.
6. On a computer on the LAN, visit `http://<linux-server-LAN-IP>:8080` and paste the admin token. Do **not** forward TCP/8080 at your router. Forward only the individual game ports you choose to make available to friends.
7. If your host firewall filters Docker traffic, verify the LAN can reach TCP/8080 and the desired game ports work externally. Docker port publishing can bypass some host firewalls such as UFW: do not rely solely on UFW deny rules to protect Docker-published ports.

## MVP limitations

Server-pack ZIPs **must** include a top-level `server.jar` and must already be installed. Forge/Fabric/NeoForge setup scripts and CurseForge *client-export* ZIPs are not supported yet. World ZIPs need exactly one `level.dat`; the whole save folder is imported, but singleplayer-host UUID reconciliation has not been implemented. Initial Minecraft runtime image uses Java 21, so some packs will need Java/version selection later. Player allowlist is enabled; player management is not yet exposed by the portal. No automated backups, deletion, rate limiting, or comprehensive testing.

## Security tradeoffs for a private network

The portal stays on a trusted LAN, so there is **no requirement** to build a public web service or expose portal TCP/8080 to the internet. This does not eliminate all security considerations: the backend mounts the Docker socket (effectively host-root-equivalent control), uploaded modpacks execute third-party Java code in Minecraft containers, and the admin token travels over plain HTTP unless local HTTPS is separately configured. For now, only grant the token to trusted household users and use trusted modpack sources. A dedicated restricted Docker worker is still desirable before broader household deployment but is less urgent than it would be for an internet-exposed administration site.

Future improvements: pack installer/version detection, proper per-user accounts, resource/port quotas, allowlist controls, offline and singleplayer UUID migration, and world backup/restore.
