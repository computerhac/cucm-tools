# CUCM Tools

A local web application for Cisco Unified Communications Manager (CUCM) administration. Provides tools that fill gaps in the native CUCM interface — searching the route plan by description, finding all phones on a subnet, migrating phones between models, bulk-updating speed dials and BLFs, auditing assigned DIDs against an import file, and migrating analog gateways from SCCP/MGCP to SIP without losing per-port configuration.

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

### Route Plan Audit
Upload a CSV or Excel file of DIDs and compare against what's actually provisioned in CUCM. Returns a per-DID report (assigned / unassigned / not in CUCM at all) downloadable as XLSX so you can reconcile a carrier export against the dial plan.

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

### Speed Dial / BLF Updater
Preview and bulk-update speed dials and Busy Lamp Field (BLF) buttons across many phones at once. Paste a list of MAC addresses, preview the current per-phone configuration, then add, change, or remove entries by button index or by matching the existing destination. Supports both single-cluster and cross-cluster operation.

### Gateway Migration
Migrate Cisco analog voice gateways from **SCCP (VG224)** or **MGCP (VG3xx)** to **SIP (VG410 / VG420 / VG450)** without losing per-port configuration. Because CUCM-managed SIP analog endpoints don't support shared lines, each port is deprovisioned on the source and immediately re-provisioned on the SIP target — one port at a time or all at once.

**Per-port dashboard** — side-by-side view of every populated source port and its target slot, with per-row Migrate / Rollback buttons and an editable target port number. Failed ports are flagged in red with the live AXL error so they can be retried.

**Physical-position preservation** — each source port maps to the same chassis-global position on the target (e.g. port 1 of card 2 on a 72+72 VG350 → target port 73) so an amphenol cable swap keeps house cabling aligned. Empty source positions stay empty on the target.

**Auto-detect everything** — for SCCP, the SKIGW gateway record is looked up via AXL `getGateway` so card sizes come from CUCM's own chassis layout (no need for the user to specify port counts). For MGCP, the same path is used with the domain name. For SIP analog target chassis, layouts for VG410 24/48 and VG420 144 are verified canonical; VG450 144 is included as a placeholder.

**Full field passthrough** — every device-level attribute the source had (owner user ID, device trust mode, common phone profile, AAR settings, MLPP flags, vendorConfig, etc.) flows to the SIP target. CUCM's per-product field validator drops anything the SIP analog product doesn't accept; everything else round-trips. Line-level fields like alerting name, voicemail profile, max calls, ring settings, AAR overrides, and recording flags are also preserved.

**Backup + restore** — before starting a migration, download a JSON backup of the full source state (chassis + every populated port's device + lines + chassis bind). If the app, browser, or migration is interrupted mid-flow, upload the JSON later to recreate any deprovisioned source artifacts. The backup is enough to restore even if the entire gateway record was deleted — `addGateway` + per-port re-bind both work from the captured data.

**SIP target shell creation** — pick VG410 / VG420 / VG450 + chassis variant from a dropdown and create the target gateway record straight from the UI before migrating.

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

For write operations (Phone Model Migration, Speed Dial / BLF updates, Gateway Migration, backup/restore), the account also needs **Standard CCM Admin Users** or equivalent write permissions. Gateway Migration additionally uses `executeSQLUpdate` (for the `mgcpdevicemember` chassis binding) — make sure the AXL role allows it.

## Test Data Utilities

For repeatable testing of the Gateway Migration tool against fresh analog gateways, the `test_data/` directory has CLI scripts that go around CUCM's BAT GUI:

- `generate_bat_csvs.py` — produces six BAT "Insert Gateways" CSVs (VG224 / VG310 / VG350 × SCCP / MGCP) with ~80% port population so you can exercise the empty-port handling.
- `clone_bat_phones.py` — reads one BAT phone-template seed device you've built in the GUI and clones it across the rest of the chassis ports via direct SQL inserts. Skips the GUI's per-port clicking. Idempotent (re-running is safe) and self-cleaning. See `test_data/README.md` for usage.

## Data Storage

- Cluster names, hostnames, ports, and usernames are stored in a local SQLite database (`clusters.db`).
- Passwords are encrypted with a Fernet key derived from your master password via PBKDF2-HMAC-SHA256 (600,000 iterations). The key is never stored on disk.
- `salt.bin` and `verifier.bin` are created on first run alongside the database. Both should be kept — losing them requires a reset.
- No data is ever transmitted anywhere other than directly to your CUCM publisher over HTTPS.

## Project Structure

```
cucm-tools/
├── launch.py                  # Entry point — password unlock, then starts the server
├── main.py                    # FastAPI app, cluster management API
├── axl.py                     # AXL SOAP client (reads via getPhone/SQL, writes via addPhone)
├── database.py                # SQLite + PBKDF2-derived Fernet encryption
├── models.py                  # Pydantic request/response models
├── routers/
│   ├── route_plan.py          # Route Plan Search API
│   ├── route_plan_audit.py    # Route Plan Audit (CSV/XLSX in/out)
│   ├── device.py              # Phone Model Migration API
│   ├── subnet.py              # Subnet Search API
│   ├── sd_updater.py          # Speed Dial / BLF Updater API
│   └── gateway_migration.py   # Gateway Migration + backup/restore API
├── templates/
│   └── index.html             # Single-page UI (tabs for each tool)
├── static/
│   └── style.css              # Styles
├── test_data/
│   ├── generate_bat_csvs.py   # Produce BAT Insert-Gateways CSVs
│   ├── clone_bat_phones.py    # Populate a BAT phone template via SQL
│   └── README.md              # BAT template setup steps
├── run.sh                     # Linux/macOS launcher
└── run.bat                    # Windows launcher
```

## License

MIT — see [LICENSE](LICENSE).
