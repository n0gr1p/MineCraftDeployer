# MineCraftDeployer

A **self-hosted, LAN-only management portal for Minecraft Java Edition servers**. A trusted household administrator can upload a modpack, choose which server/world to play, read startup logs, manage the whitelist, and grant or revoke operator privileges from a browser.

**Project status:** Early proof of concept; running in a real household with a Minecraft 1.20.1 Fabric modpack. Automated tests use simulated Docker/Minecraft interactions. Player-management changes still need live acceptance testing; do not mistake passing unit tests for a completed production rollout.

- Install several independent server profiles; **only one can run on the public Minecraft port at a time**.
- Switch profiles without changing the address friends use to join.
- Keep each profile's mod files, configs, saves, whitelist and operators on disk.
- Permanently remove unused inactive servers through an explicit, guarded Delete Server action.
- Create and list consistent **offline** profile backups on a separately mounted disk, with an identity marker and SHA256 manifest.
- Manage Java player access and operator status while the active Minecraft server is ready.
- Execute Minecraft server commands in a restricted-to-the-game **Console** panel, displaying command responses.
- Change common `server.properties` values through GUI toggles, dropdowns, and number inputs.
- Change a server's RAM allocation after installation, independently for each profile.
- Install from a CurseForge URL, **Feed The Beast (FTB) URL and pinned version ID**, or supported server ZIP (including Fabric ServerPackCreator and Dregora).

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
3. **RAM (GB):** initial heap size for the game, between 2 GB and `MCD_MAX_RAM_GB`. You can change this later without reinstalling.
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
| **RLCraft Dregora** official Forge 1.12.2 server ZIP with its `SERVER README OR DIE.txt` and `mods/` + `config/` | Supported by the specialized Forge 2860 + Java 8 runtime configuration; live verification pending |
| **FTB modpack URL** with a pinned FTB version ID | Supported through `TYPE=FTBA` and selected Java runtime; live verification pending |
| Other Forge/NeoForge ServerPackCreator scripts or arbitrary installer scripts | Not supported |

For Fabric ServerPackCreator packages, MineCraftDeployer reads `variables.txt`. It does **not** execute the uploaded `start.sh`, Java installer, or `JAVA_ARGS`: the configured RAM setting takes precedence. A specific CurseForge file/version URL is preferable to a generic modpack page when reproducibility matters.

Server ZIP uploads are limited to **4 GiB compressed**, with limits on the number and expanded size of archive entries. A successfully uploaded profile may still fail during game startup if the mods/versions are incompatible.

### Feed The Beast (FTB) modpack URL — no server ZIP needed

The **FTB app/server installer** can deploy modpacks directly by numeric FTB modpack ID and version ID. The official Minecraft Docker image supports this through `TYPE=FTBA` (the trailing **A** is important), which downloads the FTB installer and the appropriate mod loader without requiring an uploaded server ZIP.

Under **Install a new server**:

1. Enter the server name and allowed player usernames as usual.
2. Set the Minecraft heap RAM; FTB Evolution 2 recommends **8 GB**.
3. Paste the modpack URL into **OR Feed The Beast (FTB) modpack URL**. Example: `https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2`.
4. Enter the **numeric version ID** from the modpack's **Versions** tab. For FTB Evolution 2 v1.4.0, that is **100566**. The modpack ID **135** is automatically extracted from the URL. Version ID is mandatory by design, to prevent the Minecraft world from being unexpectedly upgraded merely by restarting.
5. Select the **Java version** listed under FTB's modpack Requirements. FTB Evolution 2 (Minecraft 26.1.2 / NeoForge) requires **Java 25**. Older FTB packs might require 21, 17, or 8. Do not pick the Java version by guessing.
6. Click **Install Server**. The profile is created **offline**, with its requested pack/version saved in the profile metadata. The portal does **not** download the modpack during installation.
7. Before switching to it, ensure the selected Java runtime image is present on your host (for Evolution 2: `sudo docker pull itzg/minecraft-server:java25`). Docker image pulling on activation is not implemented.
8. Select the profile and click **Make Active** when it is okay to stop the previous Minecraft server. The Docker runtime will download and install that exact FTB modpack version to its own isolated profile directory. Monitor **Logs** and wait until Minecraft reports it is ready. A running container does not prove the game has finished installing or loading.

For Evolution 2:

| Item | Value |
| --- | --- |
| FTB pack ID | `135` |
| FTB version ID | `100566` (v1.4.0, as of 19 October 2026) |
| Minecraft | `26.1.2` |
| Mod loader | NeoForge |
| Java | `25` |
| RAM | 8 GB heap recommended, 9 GB container memory limit |

References: [FTB Evolution 2 official page](https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2), [FTB Evolution 2 versions](https://www.feed-the-beast.com/modpacks/135-ftb-evolution-2/versions), and [Minecraft Docker FTBA documentation](https://docker-minecraft-server.readthedocs.io/en/latest/types-and-platforms/mod-platforms/ftb/).

**Important:** Live acceptance is still pending; the automated suite verifies the exact Docker configuration with a fake Docker daemon, not the official FTB installer downloading and booting the full pack. FTB’s external installer and download service must be reachable from your host. This cannot be fully verified before the first activation. Switching to Evolution 2 will disconnect everyone playing on the previously selected Minecraft server.

### RLCraft Dregora (Minecraft 1.12.2, Forge)

For **RLCraft Dregora v1.1.2b**, select its dedicated **server pack** ZIP (not the smaller CurseForge client export) in the **OR ready-to-run server ZIP** upload field. The specific server pack is [CurseForge file 7285709](https://www.curseforge.com/minecraft/modpacks/rlcraft-dregora/files/7285709).

The Dregora server ZIP has no standalone `server.jar`: its bundled `SERVER README OR DIE.txt` instructs administrators to install **Forge 1.12.2-14.23.5.2860 with Java 8**. MineCraftDeployer now recognizes this exact pack layout from its README plus `mods/` and `config/`. It launches the official Minecraft Docker image in Forge mode instead of running any scripts supplied inside the upload. It supports the regular root layout and one enclosing directory.

**Critical world-generation settings** are set through Docker's `server.properties` environment variables:

| Property | Configured value | Reason |
| --- | --- | --- |
| `level-name` | `DregoraRL` | Required for the Dregora world |
| `level-type` | `OTG` | Uses OpenTerrainGenerator |
| `allow-flight` | `true` | Supports flying mounts/abilities |
| `difficulty` | `hard` | The pack's intended balance |
| `max-tick-time` | `-1` | Prevents Forge watchdog shutdown during long OTG world generation |
| `enable-command-block` | `true` | Required for some structure/villager spawning |
| `view-distance` | `10` | Intended compatibility (lower values can interfere with mob spawning) |

**Never change `level-name` or `level-type` after world creation.** In this release, these are fixed specifically for Dregora profiles, not user-editable. An optional world ZIP is extracted under `DregoraRL/`; only import a world that is actually compatible with this Dregora generation setup.

The pack author warns that the **first startup appears frozen** and should be given time to finish. `max-tick-time=-1` disables Minecraft's automatic stalled-tick watchdog, so watch logs and resource usage rather than assuming `running` means healthy. The author recommends **about 8 GB of server RAM**. If you choose to pre-generate terrain for a large group, follow the bundled README and make a backup first; large-scale 20,000-block-radius pre-generation is **not** automatic.

**Before activating Dregora for the first time**, ensure the Java 8 game image is available on the Docker host:

```bash
sudo docker pull itzg/minecraft-server:java8
```

This only downloads the image; it doesn't stop the existing Minecraft server. Initial Forge installation may also download Forge libraries at runtime. **Live acceptance with the actual 644.8 MB ZIP is still pending**; tests use minimal simulated server packs and a fake Docker daemon.

### Select, start, stop, restart, and inspect logs

- **Make Active:** selects an installed server and starts it on the fixed game port. If another profile is running, it is stopped first. Wait for Minecraft to become ready.
- **Start:** starts the selected server if it is stopped/exited.
- **Stop:** gracefully stops the selected server, leaving it selected for later play.
- **Restart:** restarts the selected running server. It disconnects players.
- **Refresh:** updates status in the portal.
- **Logs:** shows recent output from that profile's Docker container. Use this first when the container exits or players cannot connect.
- **Console:** execute commands on the selected, running Minecraft server (once RCON is enabled), with in-page results. This grants full Minecraft administrator command access.
- **Settings:** edit common `server.properties` values through a friendly form. Settings are applied when Minecraft next starts or restarts, not instantly.
- **Delete Server:** permanently removes an *inactive, fully stopped* profile, its Docker container and its saved files, after exact-name confirmation. Not available on the selected server.

Players always use the **same Minecraft address and port**; the portal changes which profile is currently serving that address. **Stopped worlds remain on disk.**

### Minecraft console — commands and responses

Each installed profile has a **Console** button. To execute a command, the server must be **selected, running, and RCON-enabled**. Enter one Minecraft server command in the input and click **Execute**. For example:

```text
list
say Server maintenance in 5 minutes
time query daytime
```

The portal returns the Minecraft console's response in the panel (if a command produces no response, it reports that no text was returned). The command box accepts the leading `/` familiar from in-game commands but removes that slash before invoking Minecraft's console. It accepts **one command at a time**, up to 256 characters; it refuses embedded newlines/control characters.

The endpoint uses an authenticated Docker exec of `rcon-cli` with the command passed as **a single argument**, never a host shell or Docker command. **You cannot run Linux commands through this input.** RCON port 25575 is still **not published** to the LAN or internet.

**Treat Console as full Minecraft administrator access.** Commands can affect worlds, players, inventories, and server availability (including stopping Minecraft). There is no command allowlist, secondary role, replay log or undo. A command's console response is not proof that a modification was safe or successful. Only trusted portal users should have the access token. On older game containers, use **Players → Enable Players** for the one-time RCON migration.

### Server settings — GUI for server.properties

Each server's **Settings** panel contains supported `server.properties` controls:

| Category | Controls |
| --- | --- |
| Player experience | **PvP on/off**, **Difficulty** (Peaceful/Easy/Normal/Hard), **Default game mode** (Survival/Creative/Adventure/Spectator) |
| Gameplay | **Allow flight**, **Command blocks**, **Spawn hostile mobs**, **Spawn animals**, **Spawn NPCs/villagers**, **Allow Nether** |
| Capacity and performance | **Maximum players** (1–100), **View distance** (3–16 chunks), **Spawn protection radius** (0–128) |

The editor reads valid existing `server.properties` values, then overlays the settings you've saved in the portal. You can change settings on **inactive or running** profiles without interrupting an active game. Click **Save Settings**; the profile's desired settings are saved persistently in `meta.json`. The next time the game container is started or **Restart** is clicked, MineCraftDeployer compares the desired settings with its current container, **recreates only that game container if needed**, and passes the edited values to the official image, which updates `server.properties` during initialization. **Until restart, the existing Minecraft server still uses its previous settings.** A pending-settings message reminds you of this.

The portal does **not** provide a raw file editor or expose arbitrary Java/server options. It intentionally excludes `online-mode`, whitelist/RCON credentials, world names, world generation, and other values that could be unsafe or undermine player authentication. For whitelist and operators, use **Players**. Minecraft Java dedicated servers do **not** have an `enable-cheats` property: grant **OP** to a trusted player when they need commands.

**Dregora safeguards:** this modpack requires Hard difficulty, flight enabled, command blocks enabled, view distance 10, and its world-generation values `level-name=DregoraRL`, `level-type=OTG` and `max-tick-time=-1`. Settings that overlap the first four are shown as **modpack locked**; the other world-generation properties are not editable. These are important for world correctness.

As with RAM edits, the settings change is not a live Minecraft command: **restarting the game disconnects players**. Back up valuable worlds before risky adjustments to gameplay or modpack behavior. Tests use simulated Docker/RCON and do not establish that every mod honors every property.

### Delete an old server

Under **Installed servers**, find the server you no longer want and choose **Delete Server**. This control is available **only when the profile is not selected** and its Docker game container is fully stopped (or no container was ever created).

**This action is permanent and is intended to reclaim disk space.** It removes the Docker game container and the entire profile directory, including all world saves, mod files, settings, whitelist, operators, and profile metadata. **There is no recycle bin and no automatic backup.**

1. If the unwanted profile is selected, **Make Active** a *different* installed profile first; the portal will stop the old one gracefully. You cannot delete the currently selected profile, even when stopped.
2. Before deletion, back up anything you might want to keep. The [manual backup procedure](#back-up-your-data-manual-recommended-before-upgrades) below copies the complete profile data root while Minecraft is stopped.
3. Click **Delete Server** beside the unwanted inactive profile. Read the permanent-loss warning.
4. **Type the exact server name** shown in the portal. The delete button stays disabled until it matches exactly, including capitalization.
5. Click **Permanently Delete Server** and accept a final browser confirmation.

The server checks the active selection and current Docker status **again under the deployment lock** before deleting anything. It also requires the authenticated portal token and the exact server name. A running, paused or restarting container is refused; the portal will not stop or delete it on your behalf. If Docker cannot remove the stopped container, the profile data remains intact. If filesystem deletion fails midway, the portal reports an error rather than claiming success: inspect the folder before retrying.

**No other installed server is stopped, deleted, or reconfigured as part of this operation.** Deleting an old server is not the same as stopping a server; stopping always preserves its files.

### Change RAM after installation

Under **Installed servers**, click **Edit RAM** for the profile you want to change. Enter a whole number from **2** to your configured `MCD_MAX_RAM_GB` (12 GB by default), then click **Save RAM**.

- **Running active server:** a confirmation explains that Minecraft will stop and restart. Anyone connected will be disconnected; choose a quiet time. The portal saves the new setting and recreates **only that Minecraft game container**, preserving the bind-mounted modpack/world files. Wait for Minecraft's ready message in **Logs** afterward.
- **Stopped or inactive profile:** the new setting saves immediately, with **no game restart**. If an old Docker container already exists, the portal shows **applies on next start** until it recreates that container during activation. A new profile with no container simply uses the selected RAM on first start.
- **No change:** selecting the existing value doesn't restart the server. The backend rejects a running-server change unless the client explicitly confirms the restart.
- **If restarting with new RAM fails:** the portal attempts to restore the old saved RAM setting and relaunch the previous allocation. This rollback is **best-effort**, not a guarantee of game availability. Read the error and logs before further action; world files aren't intentionally deleted.

The RAM number configures Minecraft's Java heap (`MEMORY`) **and** a Docker memory cap of approximately that number **plus 1 GiB** for non-heap overhead. Raising RAM beyond the host's available memory can cause system slowdowns or out-of-memory failures. Ensure enough physical RAM remains for Linux, Docker, and other running services.

**You do not need to reupload a modpack or world when changing RAM.**

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

### Per-server offline backups (portal)

The **Backups** panel creates and lists complete backups of a *stopped* Minecraft server. It includes `minecraft/` (worlds, mods, configs) and `meta.json`. Archives are uncompressed `.tar` files with SHA256 manifests, stored outside the Minecraft data directory. The archive is not deleted when the server profile is permanently deleted.

**Set up a separately mounted backup drive before enabling the feature:**

1. Mount the backup disk first and check it with `findmnt` or `mountpoint`. For the home server, the ext4 volume is mounted at `/home/peter/bulk-storage` with UUID `c3eac39c-a160-4a6d-b816-6c6e599224e8`. The backup folder is `/home/peter/bulk-storage/minecraft-backups`.
2. While the actual drive is confirmed mounted, create its identity marker *inside the backup folder* (not in `~` or under `MCD_HOST_DATA`):

   ```bash
   printf '%s' 'c3eac39c-a160-4a6d-b816-6c6e599224e8' > /home/peter/bulk-storage/minecraft-backups/.minecraftdeployer-volume-uuid
   ```

3. In the existing local `.env`, set:

   ```dotenv
   MCD_BACKUP_HOST_DIR=/home/peter/bulk-storage/minecraft-backups
   MCD_BACKUP_VOLUME_UUID=c3eac39c-a160-4a6d-b816-6c6e599224e8
   ```

4. Rebuild **only the portal** using `sudo docker compose up -d --build`. Check `sudo docker compose config -q` first. The Compose bind mount exposes that folder as `/backups` inside the portal container.
5. Stop the Minecraft server you want to back up, wait for Docker to report `exited`, and open its **Backups** panel. Click **Create Offline Backup**. Do not start, delete, or modify that server while the snapshot runs. Its snapshot will be listed with a timestamp, size, and abbreviated checksum afterward.

**Safety:** The backup API requires authentication, enforces the global deployment lock, and refuses running/restarting/paused game containers. The destination must be an actual Docker bind mount, configured via both variables, with a plain identity marker matching the expected UUID. A missing drive/marker fails closed; the portal does **not** create a backup directory on the system SSD as a fallback. Unfinished `.partial` files are discarded. No automatic deletion/retention is performed. Backups may include sensitive modpack configuration; keep the backup disk and portal access protected.

**Current limitations:** This is the first offline-only backup slice; **automatic scheduling, live RCON save-coordination, restore operations, and full live acceptance testing are pending**. It does not pause or interrupt a running Minecraft server to make a backup. Backups do not include your separate `.env` file, and the marker verifies your configuration, not a cryptographically authenticated physical disk.

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
| `No such image: itzg/minecraft-server:java17` or `java8` | The Docker daemon has not downloaded the selected runtime image. Run `sudo docker pull itzg/minecraft-server:java17` for Fabric/Java 17 or `sudo docker pull itzg/minecraft-server:java8` for Dregora, then retry **Make Active**. Automatic image pulling on activation is not yet implemented. |
| `exec /image/scripts/start: operation not permitted` | Early prototype containers included `no-new-privileges`, which caused this exact error under Snap Docker. Update the portal; its next **Start/Make Active** can replace the obsolete container while preserving the bind-mounted data. Don't recursively change permissions on game files. |
| `This ZIP does not contain a top-level server.jar` | Older portal builds required a ready-to-run JAR. Update to a build supporting Fabric ServerPackCreator and the nested-ZIP wrapper. If it still rejects the pack, it may use an unsupported installer format. |
| Minecraft container says `running` but players cannot join | Minecraft may still be downloading/installing Fabric or loading mods. Open **Logs** and wait for its ready message. Also check server errors, whitelist, LAN connectivity and port forwarding. |
| Server container shows `exited` | Open **Logs** and read the last relevant error before trying to restart. This is not automatically a network problem. |
| RAM shows **applies on next start** | The profile has a saved heap size that differs from its existing stopped Docker container. Select/Start the server when convenient; the new container will use the saved RAM. |
| New RAM setting fails to restart the server | Read the error and logs. The portal attempts to restore the previous RAM setting and service, but recovery is best-effort. Verify server readiness before inviting players. |
| Player action says the game console isn't ready | The container may be running while Minecraft/RCON is still initializing. Wait for the startup-ready message, then try again; do not repeatedly restart. |
| **Players** asks for one-time restart | Expected for profiles created before this feature. Confirm at a convenient time when friends aren't playing. |
| TCP `25565` already in use | Run `sudo ss -ltnp` and look for `:25565`. Stop an unrelated **old Minecraft service** only after confirming what owns the port. |
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
| `MCD_MAX_RAM_GB` | Maximum heap size selectable when creating **or editing** a server's RAM | `12` |
| `CF_API_KEY` | Optional CurseForge API key for CurseForge URL imports | Empty |
| `MCD_BACKUP_HOST_DIR` | Optional absolute path to separately mounted backup folder on Linux host; disabled by default | `/home/peter/bulk-storage/minecraft-backups` |
| `MCD_BACKUP_VOLUME_UUID` | Required expected ext4 UUID marker for backup destination; disabled by default | `c3eac39c-a160-4a6d-b816-6c6e599224e8` |

The portal binds TCP **8080** at the configured local IP. Game containers publish the chosen Minecraft port. Changing server ports or runtime security policies can cause the portal to replace old Docker game containers on their next activation, preserving data in `MCD_HOST_DATA`.

## Security and known limitations

- **The portal is an administrator tool, not a public website.** The management token grants broad powers, including OP, and there is no account system, role separation, or request rate limiting.
- The portal mounts `/var/run/docker.sock`, which is effectively **root-level control of the Docker host**. Keep it on a trusted LAN; don't share the token with strangers or expose port 8080 on the router. The new Console feature also grants unrestricted **Minecraft** operator-level command execution, but never Linux shell access.
- Plain HTTP sends the token over the LAN without encryption. For access from outside the trusted network, do not simply forward the portal port; use a properly authenticated/encrypted private access solution instead.
- The Minecraft game containers intentionally don't set `no-new-privileges` due to the observed image startup failure on Snap Docker. This reduces defense in depth. Use **trusted modpacks**; game mods execute code inside containers.
- Java 17/21 version detection currently applies to **ServerPackCreator Fabric** packages. Other pack formats may need manual setup or different runtimes.
- No Minecraft Bedrock/Geyser support, no general Forge/NeoForge installer handling, no automatic scheduled/live backups or restore, no online player list or player kick UI, no profile restore after permanent deletion, and no singleplayer player-UUID reconciliation.
- This is a household prototype, not a hardened multi-tenant hosting panel. Test new worlds/modpacks before placing important saves on it.

## Development and testing

The source code uses FastAPI and the Python Docker SDK, with a browser-based HTML/JavaScript portal. The game runtime comes from the `itzg/minecraft-server` Docker image.

GitHub Actions checks the Python source, Docker Compose configuration and tests. Unit tests use **fake Docker containers and simulated responses**, which validates portal logic but does **not** prove a specific modpack loads or RCON/player management works end-to-end with real Minecraft.

**Suggested first acceptance test** after an update: verify the active server reaches Minecraft's `Done` message; open **Players**; add a known Java username; verify the friend can join; grant/revoke OP to a trusted account; remove and re-add the player; restart the game; confirm the whitelist and operator state persist. Perform privilege tests on a disposable or backed-up world.

The work-in-progress changes are maintained in [draft PR #1](https://github.com/n0gr1p/MineCraftDeployer/pull/1).