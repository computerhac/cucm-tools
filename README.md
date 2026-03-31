# CUCM Tools

A local web application for Cisco Unified Communications Manager (CUCM) administration. Provides tools that fill gaps in the native CUCM interface — searching the route plan by description, finding all phones on a subnet, and migrating phones between models while preserving all compatible settings.

## Features

### Route Plan Search
Search your entire dial plan by **description** or **number/pattern** across one or more clusters simultaneously. Finds all entry types in a single query:

- Directory Numbers (DNs)
- Route Patterns
- Translation Patterns
- Transformation Patterns (Calling/Called Party)
- Call Park, Meet Me, Pickup Groups, Hunt Pilots
- Voice Mail Pilots

CUCM's built-in Route Plan Report only searches by partition or pattern. This tool searches by description — useful for finding who owns a number, tracking down patterns by site name, or auditing a dial plan.

### Subnet Search
Find all phones registered within a subnet by entering a **MAC address** (resolved to its last known IP automatically) or a **direct IP address**. Select a prefix length from /22 to /29 to define the search scope.

Results are sorted by last seen timestamp — most recently registered phones first. Displays device name, description, IP address, last seen time, and last known UCM node.

### Phone Model Migration
Look up any phone or Jabber device by **MAC address or device name**, then migrate it to a different model or device type. All compatible settings are automatically transferred to the new device:

- Device pool, calling search space, location
- Softkey template, button template, owner
- Privacy, single button barge, join across lines, built-in bridge
- Allow hoteling, CTI control, trace flag
- Device-specific settings (PC port, enhanced line mode, web access, etc.)
- All line associations with labels, display names, and external phone masks

Settings that aren't valid for the target model (security profile, MLPP features, DND options, etc.) are detected automatically and dropped — CUCM assigns appropriate defaults for the new device type.

Supports physical phones (SEP) and all Jabber device types (CSF, TCT, BOT, TAB).

### Cluster Management
Add and manage multiple CUCM clusters. Credentials are encrypted at rest and protected by a master password. Supports self-signed certificates (common in most deployments).

## Requirements

- Python 3.11 or higher
- Network access to the CUCM publisher on port 8443 (AXL/SOAP)
- A CUCM application user or end user account with the **Standard AXL API Access** role
- CUCM 15 (likely compatible with 12.5+, untested on earlier versions)

## Installation

### Linux / macOS

```bash
git clone <repo-url>
cd cucm-tools
./run.sh
```

### Windows

Double-click `run.bat`. It handles virtual environment setup automatically. Python 3.11+ must be installed and added to PATH. Download from [python.org](https://www.python.org/downloads/) — check **"Add Python to PATH"** during installation.

## First-Time Setup

On first run you will be prompted in the terminal to set a **master password**. This password encrypts your cluster credentials on disk — there is no recovery option other than resetting (which wipes all saved clusters), so keep it somewhere safe.

1. Run `run.sh` / `run.bat` and set your master password when prompted.
2. The browser will open automatically at `http://localhost:8000`.
3. Under **Clusters**, click **+ Add Cluster** and enter your CUCM publisher details.
4. Click **Test** to verify the AXL connection.
5. Use the tabs to access each tool.

The app binds to `127.0.0.1` only — it is not accessible from other machines on the network.

### Forgotten Password

If you forget your master password, type `RESET` at the password prompt. You will be asked to type `CONFIRM`, after which all saved cluster configurations are wiped and you can set a new password. Cluster credentials will need to be re-entered.

## AXL API Access

The AXL user needs the **Standard AXL API Access** role in CUCM. To set this up:

1. In CUCM Administration, go to **User Management → Application User** (or End User).
2. Add the role **Standard AXL API Access** under Permissions Information.
3. Save. Use these credentials when adding the cluster in CUCM Tools.

For Phone Model Migration write operations (creating/removing phones), the account also needs **Standard CCM Admin Users** or equivalent write permissions.

## Data Storage

- Cluster names, hostnames, ports, and usernames are stored in a local SQLite database (`clusters.db`).
- Passwords are encrypted with a Fernet key derived from your master password via PBKDF2-HMAC-SHA256 (600,000 iterations). The key is never stored on disk.
- `salt.bin` and `verifier.bin` are created on first run alongside the database. Both should be kept — losing them requires a reset.
- No data is ever transmitted anywhere other than directly to your CUCM publisher over HTTPS.

## Project Structure

```
cucm-tools/
├── launch.py            # Entry point — password unlock, then starts the server
├── main.py              # FastAPI app, cluster management API
├── axl.py               # AXL SOAP client (reads via getPhone/SQL, writes via addPhone)
├── database.py          # SQLite + PBKDF2-derived Fernet encryption
├── models.py            # Pydantic request/response models
├── routers/
│   ├── route_plan.py    # Route Plan Search API
│   ├── device.py        # Phone Model Migration API
│   └── subnet.py        # Subnet Search API
├── templates/
│   └── index.html       # Single-page UI
├── static/
│   └── style.css        # Styles
├── run.sh               # Linux/macOS launcher
└── run.bat              # Windows launcher
```

## License

MIT — see [LICENSE](LICENSE).
