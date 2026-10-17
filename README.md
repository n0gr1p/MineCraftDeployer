# MineCraftDeployer

A **self-hosted, LAN-only management portal for Minecraft Java Edition servers**. A trusted household administrator can upload a modpack, choose which server/world to play, read startup logs, manage the whitelist, and grant or revoke operator privileges from a browser.

**Project status:** Early proof of concept; running in a real household with a Minecraft 1.20.1 Fabric modpack. Automated tests use simulated Docker/Minecraft interactions. Player-management changes still need live acceptance testing; do not mistake passing unit tests for a completed production rollout.

- Install several independent server profiles; **only one can run on the public Minecraft port at a time**.
- Switch profiles without changing the address friends use to join.
- Keep each profile's mod files, configs, saves, whitelist and operators on disk.
- Manage Java player access and operator status while the active Minecraft server is ready.
- Install from a CurseForge URL, a supported server ZIP, or a Fabric ServerPackCreator package (including a ZIP wrapped inside a ZIP).

**This is not a hosting service.** You supply the Linux machine, Docker, storage, and (for remote friends) router configuration. Minecraft Java Edition only.

## Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Using the portal](#using-the-portal)
5. [Whitelist and operator management](#whitelist-and-operator-management)
6. [Updates and backups](#updates-and-backups)
7. [Troubleshooting](#troubleshooting)
8. [Configuration reference](#configuration-reference)
9. [Security and known limitations](#security-and-known-limitations)
10. [Development and testing](#development-and-testing)

## How it works

| Component | Address/port | Internet router forwarding |
| --- | --- | --- |
| **Web management portal** | `http://<linux-LAN-IP>:8080`, bound to the configured local IP | **Never forward TCP 8080** |
| **Active Minecraft game** | `<public-host>:25565` by default | Forward **TCP 25565 only**, if friends play from outside the LAN |
| **Inactive Minecraft profiles** | No published game server | None |
| **RCON management** | Inside the active Minecraft container | **Never forward TCP 25575** |

Only the selected Minecraft profile should be active; all profiles share the same game port. Switching uses a deployment lock, gracefully stops the old game, and then starts the new one. The portal tries to restore the previous game if switching fails, but this is **best-effort**, not a transaction or backup system.

Two types of Docker container are involved: the **portal** (`minecraftdeployer-portal-1`) and the Minecraft game containers (`mcd-<profile-id>`). Rebuilding the portal doesn't inherently restart the separate Minecraft game container.

**Important:** A Docker status of `running` only means the container is running. Players should wait until Minecraft logs confirm that the server has finished starting (usually a `Done (...)!` message).

## Requirements

- A Linux host with Docker and Docker Compose v2, plus Git for checkout and updates.
- Enough free RAM and disk space for the selected modpack. The web form defaults to 6 GB Minecraft heap; large modpacks may require more. The runtime container is capped at roughly the selected RAM **plus 1 GB**.
- A supported, trustworthy Minecraft Java Edition modpack or server ZIP.
- An available TCP **25565** game port (or your configured game port). Do not leave an old stand-alone Java Minecraft server bound to it.
- A private LAN for the management portal. For friends connecting from the internet, a public IP/DNS and router forwarding **for the Minecraft port only**.
- Agreement to the Minecraft EULA; the game runtime sets `EULA=TRUE`.

The Minecraft Java runtime runs in Docker; you do **not** need to install Java on the Linux host.

### Docker installed using Snap (Ubuntu)

Snap Docker may be restricted from binding host directories such as `/srv`, even when `sudo mkdir` works. On our tested Snap Docker host, `/srv/minecraft-deployer/data` failed with a **read-only file system** error when Compose created the bind mount.

For Snap Docker, prefer a data folder inside the real Linux user's home directory, for example:

```bash
mkdir -p /home/<your-username>/minecraft-deployer-data
```

Set the **full absolute path** in `.env`:

```dotenv
MCD_HOST_DATA=/home/<your-username>/minecraft-deployer-data
```

Substitute your actual username; do not include literal angle brackets. Check `snap connections docker` if Docker still cannot access the home directory. Use `sudo docker ...` below if your Docker installation requires it.

For a conventional Docker Engine installation, a suitable writable directory under `/srv` is fine. The portal's Docker socket can create/manage game containers, so it must be used only on a trusted host.

## Installation

These instructions use the current **`feat/self-service-mvp`** branch while PR #1 remains unmerged.

### 1. Clone and check the environment

```bash
git clone --branch feat/self-service-mvp --single-branch https://github.com/n0gr1p/MineCraftDeployer.git
cd MineCraftDeployer
docker --version
docker compose version
```

Use `sudo docker` instead if that's how your installation is configured.

Check the host's LAN address with `hostname -I` or `ip -4 addr`. Make sure nothing else is using the Minecraft port before the first activation.

### 2. Configure the portal

```bash
cp .env.example .env
openssl rand -hex 32
```

Open `.env` in your editor. **Copy the random value** from the last command into `MCD_TOKEN`; do not keep the sample placeholder. Set at least:

```dotenv
# LAN IP of the Linux machine running Docker:
MCD_PORTAL_BIND_IP=192.168.1.50

# Random secret generated above, not this example:
MCD_TOKEN=REPLACE_WITH_YOUR_RANDOM_SECRET

# Choose a writable absolute path. For Snap Docker, use your home directory:
MCD_HOST_DATA=/home/youruser/minecraft-deployer-data

# Fixed Minecraft game port:
MCD_GAME_PORT=25565

# IP or DNS name that Minecraft players use to join:
MCD_PUBLIC_HOST=192.168.1.50

# Maximum selectable Minecraft RAM in GB:
MCD_MAX_RAM_GB=12

# Optional CurseForge API key:
CF_API_KEY=
```

These are **illustrative values**, not values that will fit every network. For outside friends, change `MCD_PUBLIC_HOST` to your internet-facing DNS name or address; this setting determines the address shown in the portal, not the router configuration.

Create the chosen data directory **before** running Compose:

```bash
mkdir -p /home/youruser/minecraft-deployer-data
chmod 600 .env
```

If you chose another `MCD_HOST_DATA` location, create that location instead. Keep `.env` private: it holds your management token.

### 3. Build and start the portal

From the repo directory:

```bash
sudo docker compose config -q
sudo docker compose up -d --build
sudo docker compose ps
```

A healthy portal should answer:

```bash
curl -fsS http://192.168.1.50:8080/api/health
```

Replace `192.168.1.50` with the actual value of `MCD_PORTAL_BIND_IP`. The expected result is `{"ok":true}`.

### 4. Sign in

Open `http://<linux-LAN-IP>:8080` from a browser **on the private LAN**. Paste the `MCD_TOKEN` value from your `.env` into **Portal access token**, then click **Connect**.

The portal uses HTTP, not HTTPS, by default. Don't expose this management page to the public internet and don't share its access token with ordinary Minecraft friends.

### 5. Verify a game locally first

Install a server profile, select **Make Active**, open **Logs**, and wait for Minecraft's finished-startup message. Connect to the game using its **LAN IP** before trying external port forwarding.

Only once the local game works should you forward TCP 25565 to the Linux host for internet players. Never forward portal TCP 8080 or RCON TCP 25575.

## Using the portal

### Install a server profile

Under **Install a new server**, fill in:

1. **Server name:** a friendly label such as `Family Adventure`.
2. **Allowed Minecraft Java player names:** 1–50 Minecraft **Java** usernames, separated by commas or spaces. Names must be 3–16 letters, numbers, or underscores. **Periods, spaces within names, and Xbox gamertags are not valid** in this Java-only implementation.
3. **RAM (GB):** heap size for the game, between 2 GB and `MCD_MAX_RAM_GB`.
4. **Exactly one modpack source:** either a valid HTTPS CurseForge Minecraft modpack URL **or** a supported server ZIP.
5. **Optional saved world ZIP:** a world containing **exactly one** `level.dat` somewhere in the archive. The containing save folder becomes the server's `world` directory.

Click **Install Server**. Uploading creates an **installed/offline profile**; it does **not** immediately start a playable server. For a new game, the server generates its own world when first launched.

**Server ZIP formats currently supported:**

| ZIP contents | Support |
| --- | --- |
| Ready-to-run `server.jar` at the top level | Supported; defaults to Java 21 |
| Fabric ServerPackCreator `variables.txt`, with Minecraft version, Fabric loader version, and recommended Java 17 or 21 | Supported; the Docker image installs the Fabric runtime |
| One outer ZIP containing the supported inner server ZIP | Supported; automatically unwrapped (e.g. Prominence II / Hasturian Era) |
| CurseForge **client export** (not a dedicated server pack) | Not supported |
| Forge/NeoForge ServerPackCreator scripts or arbitrary installer scripts | Not supported |

For Fabric ServerPackCreator packages, MineCraftDeployer reads `variables.txt`. It does **not** execute the uploaded `start.sh`, Java installer, or `JAVA_ARGS`: the configured RAM setting takes precedence. A specific CurseForge file/version URL is preferable to a generic modpack page when reproducibility matters.

Server ZIP uploads are limited to **4 GiB compressed**, with limits on the number and expanded size of archive entries. A successfully uploaded profile may still fail during game startup if the mods/versions are incompatible.

### Select, start, stop, restart, and inspect logs

- **Make Active:** selects an installed server and starts it on the fixed game port. If another profile is running, it is stopped first. Wait for Minecraft to become ready.
- **Start:** starts the selected server if it is stopped/exited.
- **Stop:** gracefully stops the selected server, leaving it selected for later play.
- **Restart:** restarts the selected running server. It disconnects players.
- **Refresh:** updates status in the portal.
- **Logs:** shows recent output from that profile's Docker container. Use this first when the container exits or players cannot connect.

Players always use the **same Minecraft address and port**; the portal changes which profile is currently serving that address. **Stopped worlds remain on disk.**

### Where the data lives

`MCD_HOST_DATA` contains the profile directories and the portal's active-server selection:

```text
minecraft-deployer-data/
  active.json
  <server-id>/
    meta.json
    minecraft/
      mods/
      config/
      world/
      whitelist.json
      ops.json
      ...
```

Not all files exist until Minecraft has started and generated them. Do not rename profile ID directories, delete metadata, or manually change game files while the server is running.

## Whitelist and operator management

**Players** appears under each installed server. Player changes are available only on the **selected, running Minecraft server**, once its in-game console is ready.

### First time on an existing profile

Older servers such as Meow were created with live player management disabled. Open **Players**, choose **Enable Players (restart once)**, and confirm. This **replaces that profile's game container** and disconnects current players, but preserves its bind-mounted Minecraft data/world. Wait until the server finishes starting.

New profiles have the player-management runtime enabled from creation and normally don't need this migration.

### Everyday player tasks

- **Allow a friend to join:** open **Players**, enter their exact Java username, and click **Add to whitelist**.
- **Remove access:** click **Remove** beside a player. If that player is an operator, first click **Revoke OP**.
- **Grant administrator privileges:** click **Grant OP** beside an existing whitelisted player and confirm.
- **Remove administrator privileges:** click **Revoke OP**. This doesn't remove the player from the whitelist.

**OP is powerful.** Minecraft operators can use server administration commands, including commands that affect other players and the world. Treat granting OP as an administrative action, not a normal invitation.

The portal issues restricted Minecraft commands (`whitelist add`, `whitelist remove`, `op`, `deop`) inside the running game container using Docker exec and the image's `rcon-cli`. **No RCON port is published**. Minecraft's own `whitelist.json` and `ops.json` are authoritative when present, so changes should survive game restarts and profile switches.

There is **no arbitrary console-command box**, no offline player editor, and no separate limited-permission account for a child: **anyone holding the portal access token can also grant OP**.

## Updates and backups

### Update the portal

From its Git checkout:

```bash
git pull --ff-only
sudo docker compose up -d --build
```

The first command updates checked-out code; the second rebuilds/restarts the **portal**. It does not automatically delete profile directories, worlds, mods, or saves, and does not by itself restart a separate running Minecraft game. When a newer build requires a **game-container migration**, the portal may request a separate, explicit game restart.

Review changes before pulling if you have made local edits. Keep your `.env`: it's local configuration and shouldn't be replaced by an example file.

### Back up your data (manual; recommended before upgrades)

**Automatic backups are not implemented.** For a consistent world backup, first use the portal's **Stop** button on the active server and wait for it to exit. Then, from the repo directory:

```bash
DATA_DIR=$(sed -n 's/^MCD_HOST_DATA=//p' .env)
sudo tar -czf "$HOME/minecraft-deployer-data-$(date +%Y%m%d-%H%M%S).tar.gz" -C "$DATA_DIR" .
```

This produces a timestamped archive in your Linux home directory containing installed profiles and worlds. The archive might be **large**; make sure there's enough free space. Keep a copy somewhere else and verify the archive before depending on it. The archive doesn't include `.env`; back up that file **separately and securely** if you need to preserve the portal token and deployment configuration.

Restore operations can overwrite or destroy live worlds, so this README intentionally does not provide a blind bulk-restore command. Inspect and confirm the target before restoring.

### Basic diagnostic commands

```bash
# See the portal status
sudo docker compose ps

# Portal application logs
sudo docker compose logs --tail=100 portal

# List all MinecraftDeployer game containers
sudo docker ps -a --filter name=mcd-

# Confirm the Minecraft game port listener
sudo ss -ltnp | grep ':25565'
```

Always inspect logs and preserve the data directory before deleting or recreating containers manually. Never use `docker compose down -v` or an indiscriminate cleanup command as a troubleshooting shortcut.

## Troubleshooting

| Symptom | Likely issue / safe first response |
| --- | --- |
| `mkdir ... /srv ... read-only file system` or Docker cannot mount `/srv` | Snap Docker confinement may prevent host-path mounts. Choose a writable folder under your Linux home directory, update `MCD_HOST_DATA`, and recreate **the portal only** with Compose. |
| Portal opens only from the Linux host | `MCD_PORTAL_BIND_IP` probably points to `127.0.0.1`. Set the Linux LAN IP, run Compose again, and check the host firewall. Never expose 8080 to the internet. |
| Portal says `Unauthorized` | Connect using the value of `MCD_TOKEN` from your local `.env`, not the sample placeholder. Never paste this token into public messages. |
| `No such image: itzg/minecraft-server:java17` | The Docker daemon has not downloaded that runtime image. Run `sudo docker pull itzg/minecraft-server:java17`, then retry **Make Active**. Java 21 packs may analogously need the Java 21 image. Automatic image pulling on activation is not yet implemented. |
| `exec /image/scripts/start: operation not permitted` | Early prototype containers included `no-new-privileges`, which caused this exact error under Snap Docker. Update the portal; its next **Start/Make Active** can replace the obsolete container while preserving the bind-mounted data. Don't recursively change permissions on game files. |
| `This ZIP does not contain a top-level server.jar` | Older portal builds required a ready-to-run JAR. Update to a build supporting Fabric ServerPackCreator and the nested-ZIP wrapper. If it still rejects the pack, it may use an unsupported installer format. |
| Minecraft container says `running` but players cannot join | Minecraft may still be downloading/installing Fabric or loading mods. Open **Logs** and wait for its ready message. Also check server errors, whitelist, LAN connectivity and port forwarding. |
| Server container shows `exited` | Open **Logs** and read the last relevant error before trying to restart. This is not automatically a network problem. |
| Player action says the game console isn't ready | The container may be running while Minecraft/RCON is still initializing. Wait for the startup-ready message, then try again; do not repeatedly restart. |
| **Players** asks for one-time restart | Expected for profiles created before this feature. Confirm at a convenient time when friends aren't playing. |
| TCP `25565` already in use | Check with `sudo ss -ltnp | grep ':25565'`. Stop an unrelated **old Minecraft service** only after confirming what owns the port. |
| Can't upload player name with `.` | Java names in this version permit letters, numbers, underscores only (3–16 characters). Don't silently change a real username; Bedrock/Geyser naming requires additional support. |

When asking for help, share the **error message and relevant logs**, but **redact `MCD_TOKEN` and any sensitive configuration**. A modpack can take several minutes to install on first launch. Don't assume a repeated click helps.

## Configuration reference

| Variable | Meaning | Example/default |
| --- | --- | --- |
| `MCD_PORTAL_BIND_IP` | Local host interface for the web portal; don't use `0.0.0.0` on an untrusted network | `127.0.0.1` in `.env.example`; set LAN IP to use another computer |
| `MCD_TOKEN` | Secret needed for portal actions, including OP management | Generate with `openssl rand -hex 32` |
| `MCD_HOST_DATA` | Absolute host path for permanent Minecraft profiles and worlds | `/srv/minecraft-deployer/data` conventional Docker, or `/home/<user>/minecraft-deployer-data` with Snap |
| `MCD_GAME_PORT` | The single Minecraft game port shared by all profiles | `25565` |
| `MCD_PUBLIC_HOST` | IP/DNS displayed as the game's join address | Your LAN address for initial tests, public DNS for outside friends |
| `MCD_MAX_RAM_GB` | Maximum heap size selectable at server creation | `12` |
| `CF_API_KEY` | Optional CurseForge API key for CurseForge URL imports | Empty |

The portal binds TCP **8080** at the configured local IP. Game containers publish the chosen Minecraft port. Changing server ports or runtime security policies can cause the portal to replace old Docker game containers on their next activation, preserving data in `MCD_HOST_DATA`.

## Security and known limitations

- **The portal is an administrator tool, not a public website.** The management token grants broad powers, including OP, and there is no account system, role separation, or request rate limiting.
- The portal mounts `/var/run/docker.sock`, which is effectively **root-level control of the Docker host**. Keep it on a trusted LAN; don't share the token with strangers or expose port 8080 on the router.
- Plain HTTP sends the token over the LAN without encryption. For access from outside the trusted network, do not simply forward the portal port; use a properly authenticated/encrypted private access solution instead.
- The Minecraft game containers intentionally don't set `no-new-privileges` due to the observed image startup failure on Snap Docker. This reduces defense in depth. Use **trusted modpacks**; game mods execute code inside containers.
- Java 17/21 version detection currently applies to **ServerPackCreator Fabric** packages. Other pack formats may need manual setup or different runtimes.
- No Minecraft Bedrock/Geyser support, no general Forge/NeoForge installer handling, no automatic backups/restore, no online player list or player kick UI, no profile deletion, and no singleplayer player-UUID reconciliation.
- This is a household prototype, not a hardened multi-tenant hosting panel. Test new worlds/modpacks before placing important saves on it.

## Development and testing

The source code uses FastAPI and the Python Docker SDK, with a browser-based HTML/JavaScript portal. The game runtime comes from the `itzg/minecraft-server` Docker image.

GitHub Actions checks the Python source, Docker Compose configuration and tests. Unit tests use **fake Docker containers and simulated responses**, which validates portal logic but does **not** prove a specific modpack loads or RCON/player management works end-to-end with real Minecraft.

**Suggested first acceptance test** after an update: verify the active server reaches Minecraft's `Done` message; open **Players**; add a known Java username; verify the friend can join; grant/revoke OP to a trusted account; remove and re-add the player; restart the game; confirm the whitelist and operator state persist. Perform privilege tests on a disposable or backed-up world.

The work-in-progress changes are maintained in [draft PR #1](https://github.com/n0gr1p/MineCraftDeployer/pull/1).