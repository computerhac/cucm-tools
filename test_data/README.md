# Gateway-Migration Test Data

Repeatable BAT "Insert Gateways" CSVs for testing the migration tool
against SCCP and MGCP analog gateways of varying sizes.

## Files

| CSV | Domain | Cards | Ports filled / skipped | DN range |
|-----|--------|-------|------------------------|----------|
| `vg224_sccp.csv` | SKIGWEFBEEF2240 | 1× 24 FXS (slot 2) | 20 / 4 | +10009981000–023 |
| `vg224_mgcp.csv` | VG224TEST       | 1× 24 FXS (slot 2) | 20 / 4 | +10009985000–023 |
| `vg310_sccp.csv` | SKIGWEFBEEF3100 | 1× 48 FXS (slot 2) | 39 / 9 | +10009982000–047 |
| `vg310_mgcp.csv` | VG310TEST       | 1× 48 FXS (slot 2) | 39 / 9 | +10009986000–047 |
| `vg350_sccp.csv` | SKIGWEFBEEF3500 | 2× 72 FXS (slots 2 & 4) | 116 / 28 | +10009983000–143 |
| `vg350_mgcp.csv` | VG350TEST       | 2× 72 FXS (slots 2 & 4) | 116 / 28 | +10009987000–143 |

Every 5th port (1-based) is intentionally left empty so the migration
tool's empty-slot handling gets exercised. DN partition is `dn_PT`,
CSS blank.

## Quick template population

CUCM BAT phone templates need one device entry per chassis port and
that's a lot of clicking. `clone_bat_phones.py` reads one seed device
you've already built in the GUI and clones it for every other port.

```
# VG224 SCCP — 1 card × 24 ports
python3 clone_bat_phones.py 3 ANvg224sccp-template400 2:0:24

# VG350 SCCP — 2 cards × 72 ports, chassis numbering 1..144
python3 clone_bat_phones.py 3 ANvg350sccp-template400 2:0:72,4:0:72

# Preview what would happen without writing
python3 clone_bat_phones.py 3 ANvg224sccp-template400 2:0:24 --dry-run
```

The seed's HHH suffix (e.g. `400` → slot 2, subunit 0, port 1) is
automatically excluded. Any digits in the seed line's `pattern`,
`label`, `display`, `displayAscii`, `alertingName`, or `description`
get substituted with the new chassis-global port number — so
`vg224 line 1 sccp` becomes `vg224 line 2 sccp`, …, `line 24 sccp`.

If the clones land as standalone phones rather than entries under the
BAT template, the script may need a BAT-specific AXL call; let me know
what shows up after the first run and I'll adjust.

## Step 1 — Create the BAT templates in CUCM

`Insert Gateways` reads the chassis product / slot modules from a BAT
template, not from the CSV. Create one template per (model, protocol)
pair under **Bulk Administration → Gateway Templates → Gateway Template
Configuration**.

VG224 (SCCP):
- Product: Cisco VG224
- Protocol: SCCP
- Slot 2 → ANALOG → Subunit 0 → SM-D-24FXS-SCCP

VG224 (MGCP):
- Product: Cisco VG224
- Protocol: MGCP
- Slot 2 → ANALOG → Subunit 0 → 24FXS

VG310 (SCCP):
- Product: Cisco VG310
- Protocol: SCCP
- Slot 2 → ANALOG → Subunit 0 → SM-D-48FXS-E-SCCP

VG310 (MGCP):
- Product: Cisco VG310
- Protocol: MGCP
- Slot 2 → ANALOG → Subunit 0 → 48FXS

VG350 (SCCP):
- Product: Cisco VG350
- Protocol: SCCP
- Slot 2 → ANALOG → Subunit 0 → SM-D-72FXS-SCCP
- Slot 4 → ANALOG → Subunit 0 → SM-D-72FXS-SCCP

VG350 (MGCP):
- Product: Cisco VG350
- Protocol: MGCP
- Slot 2 → ANALOG → Subunit 0 → 72FXS
- Slot 4 → ANALOG → Subunit 0 → 72FXS

For each template, set device-level defaults (Device Pool, Common Phone
Profile, Phone Button Template) once and reuse for both protocols of
the same model — CUCM will let the per-port CSV values override
description/DN/CSS/partition.

The exact subunit module names depend on which FXS module types your
cluster lists under **Module in Slot N → Subunit 0** — for MGCP they
typically don't have the `-SCCP` suffix. If the dropdown shows a
different option (e.g. `24FXS` vs `SM-D-24FXS-SCCP`), pick the one
that's there and update the template; the CSV doesn't reference the
module name so the row data stays the same.

## Step 2 — Upload each CSV

**Bulk Administration → Gateways → Insert Gateways**

1. **File Name**: pick one of the CSVs
2. **Gateway Template**: pick the matching template from step 1
3. **Run Immediately** or schedule a job
4. Watch **Bulk Administration → Job Scheduler** for completion

Repeat for each of the 6 CSVs (one per template).

## Step 3 — Verify in CUCM GUI

After each insert:
- **Device → Gateway** — the domain (SKIGWEFBEEF2240 / VG224TEST / etc.)
  should appear with the right product
- Click into it and confirm the populated slots / subunits show
  endpoint icons on the ports your CSV listed; the ~20% you skipped
  should show as empty
- **Call Routing → Directory Number** — search for `1000998*`; the
  DNs should be present in partition `dn_PT`

## Step 4 — Run the migration tool

Open the cucm-tools app, go to **Gateway Migration**:
- Source: enter the chassis MAC (`EFBEEF2240`, `EFBEEF3100`,
  `EFBEEF3500`) **or** the SKIGW name; for MGCP enter the domain
  (`VG224TEST` etc.)
- Target: create or pick a SIP analog target gateway (VG410/VG420)
- Migrate All Remaining → confirm DN moves cleanly
- Rollback All Migrated → confirm DN goes back

## Cleanup

To wipe a test gateway: **Device → Gateway → Find → check the row →
Delete Selected**. CUCM cascades the AN/AALN endpoints and DNs go back
to unassigned (delete the DN separately if needed).

## Regenerating

```
cd test_data
python3 generate_bat_csvs.py
```

Edits the existing CSVs in-place. Edit the `GATEWAYS` list in the
script to change port counts, DN ranges, or skip frequency.
