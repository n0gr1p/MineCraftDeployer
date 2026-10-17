# MineCraftDeployer

Early **proof of concept** for Minecraft Java Edition dedicated servers on Linux. Install multiple CurseForge-based server profiles with separate worlds, but run **only one at a time**, on one externally forwarded TCP port.

## Network design

The administrative website remains **LAN-only**. Your daughter installs packs, chooses the active server and controls its lifecycle from a browser in your home. Friends connect to the selected Minecraft server through **one fixed public game port**.

| Service | Binding | Router port forwarding |
|---|---|---|
| Web portal | `MCD_PORTAL_BIND_IP:8080` (Linux LAN address only) | **None. Never forward it.** |
| Active Minecraft server | `MCD_PUBLIC_HOST:MCD_GAME_PORT` (default TCP 25565) | **One TCP port, default 25565** |
| Installed inactive servers | Not listening | **None** |

Each server profile has its own saved files, mods, config and world. Choosing **Make Active** on another installed profile: 1) gracefully stops any other managed running server, 2) disables Docker restart policies on inactive profiles, 3) starts the selected profile using the same game port, and 4) records that selection on disk. Stopping the selected server keeps it selected but offline. Start/restart actions operate only on the selected profile.

The switch operation uses a Linux filesystem lock so overlapping portal requests cannot both activate different servers. If activation fails, the worker attempts to restore the previously running profile and reports a failure. The new game may still require startup time after Docker reports it as running. Existing preview containers with a different published port are recreated using the new port when selected; their bind-mounted game files remain intact.

## Quick start

1. Install Docker Engine and Docker Compose on Linux.
2. Copy `.env.example` to `.env`, set `MCD_TOKEN` using `openssl rand -hex 32`.
3. Set `MCD_PORTAL_BIND_IP` to the Linux server's LAN IPv4 address (e.g. `192.168.1.50`), not `0.0.0.0`. Its default is loopback-only.
4. Create the absolute `MCD_HOST_DATA` directory (example `/srv/minecraft-deployer/data`). Set `MCD_PUBLIC_HOST` to the name/IP your daughter's friends will join.
5. Leave `MCD_GAME_PORT=25565` unless you have a reason to change it. Forward **only TCP 25565** to the Linux host using your router. If you change the port later, switching servers will recreate older containers without deleting their game directories.
6. For CurseForge URL installs the Java 21 container image includes a shared API key; `CF_API_KEY` is optional. You can still supply your own key.
7. Run `docker compose up -d --build`.
8. From the home LAN, browse to `http://<linux-server-LAN-IP>:8080`, enter the portal token, and install any number of server profiles.
9. During installation enter at least one valid **Minecraft Java player name** (add friends separated by commas); the portal enables whitelisting using that list. Select an installed profile and click **Make Active**. Share the same `MCD_PUBLIC_HOST:MCD_GAME_PORT` address regardless of which profile is selected.
10. Verify the server log reaches Minecraft's `Done` message, then test joining locally *before* forwarding the game port to friends.

If a previous multiport preview is already running, select the desired active profile once to stop the others and normalize their restart policies. Make sure no unrelated service is already listening on the fixed Minecraft game port.

## MVP limitations

Server-pack ZIPs **must** include a top-level `server.jar` and must already be installed. Forge/Fabric/NeoForge setup scripts and CurseForge *client-export* ZIPs are not supported yet. World ZIPs need exactly one `level.dat`; the whole save folder is imported, but singleplayer-host UUID reconciliation has not been implemented. The initial Minecraft runtime image uses Java 21, which does not fit all modpacks. CurseForge URLs to a **specific modpack file/version** are recommended: a general modpack page URL can upgrade to the latest compatible pack on subsequent starts, which may change an existing world. Player allowlist is enabled; player management is not yet exposed by the portal. No automated backups, deletion, rate limiting, or real Minecraft integration testing yet.

## Security tradeoffs for a private network

The portal stays on a trusted LAN, and **TCP 8080 is never port-forwarded**. The backend presently mounts the Docker socket, granting effectively root-equivalent control of the host. Do not give the administrative token to untrusted users. Modpacks execute Java code in containers, so only install trusted content. The token travels over unencrypted LAN HTTP unless local HTTPS is configured.

Future improvements: real modpack installer detection, version-aware Java selection, world backups, UUID migration, whitelist management and resource quotas. A restricted deployment worker is desirable even on a LAN but is not a prerequisite for evaluating this private-network prototype.


## Linux test deployment (no changes to existing Minecraft servers)

Before installing, check `docker --version`, `docker compose version`, `git --version` and whether TCP/25565 or TCP/8080 are already occupied. Do **not** stop or replace any existing Minecraft service unless intentionally switching its port forwarding.

```bash
git clone --branch feat/self-service-mvp --single-branch https://github.com/n0gr1p/MineCraftDeployer.git
cd MineCraftDeployer
cp .env.example .env
openssl rand -hex 32
ip -4 addr show
```

Edit `.env` with a text editor, replace `MCD_TOKEN` with the random string, set `MCD_PORTAL_BIND_IP` to **your own** private LAN address and `MCD_PUBLIC_HOST` to the name/IP players will join. The optional `CF_API_KEY` can remain empty for Java 21. Accepting `EULA=TRUE` in the generated game container confirms Minecraft's EULA acceptance; only deploy if you agree to it.

```bash
sudo mkdir -p /srv/minecraft-deployer/data
sudo docker compose config -q
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1:8080/api/health
```

The curl command targets loopback and will only work if the portal is loopback-bound. When `MCD_PORTAL_BIND_IP` is set to your LAN address, substitute that address for `127.0.0.1`. From another computer on the same LAN, open `http://YOUR-LINUX-LAN-IP:8080` and enter `MCD_TOKEN`. Install a pack and click Make Active. The game is only joinable once the Minecraft startup log confirms it finished loading. Stop the portal for troubleshooting with `sudo docker compose stop` (this does **not** stop separately created game containers).

The Docker socket gives the portal effective administrator access to the host. Use this initial build only with trusted household users, and keep the management port off internet port forwarding. A modern supported CurseForge pack URL is the recommended initial test; generic installer-based server ZIPs are **not** supported yet.
