#!/usr/bin/env python3
"""
Generate BAT 'Insert Gateways' CSVs for repeatable migration-tool tests.

Run: python3 generate_bat_csvs.py
Writes six CSVs under ./bat_csvs/ — one per (gateway model, protocol)
combination. Re-run any time you want to recreate the test data.

Each CSV is one row per port. CUCM BAT also needs a matching BAT template
that defines the chassis (product + slot modules); see ./README.md for the
template setup steps.
"""

import os

OUT_DIR = os.path.join(os.path.dirname(__file__), "bat_csvs")
os.makedirs(OUT_DIR, exist_ok=True)

# DN format: \+1000998XXXX. The leading backslash matches the convention
# used by CUCM screenshots so BAT parses the leading '+' literally rather
# than as a column separator escape.
# Skip every 5th port (port_1based % 5 == 0) for ~80% fill with realistic
# gaps that exercise the migration tool's empty-slot handling.

GATEWAYS = [
    # (filename, domain_name, description, slots, dn_base, protocol)
    # slots = [(slot, subunit, port_count), ...]
    # VG224 — 1 NM slot, 24-port FXS card
    ("vg224_sccp.csv", "SKIGWEFBEEF2240", "VG224 SCCP test",
     [(2, 0, 24)], 1000, "SCCP"),
    ("vg224_mgcp.csv", "VG224TEST",        "VG224 MGCP test",
     [(2, 0, 24)], 5000, "MGCP"),
    # VG310 — single-slot, 48-port FXS card (SM-D-48FXS-E)
    ("vg310_sccp.csv", "SKIGWEFBEEF3100", "VG310 SCCP test",
     [(2, 0, 48)], 2000, "SCCP"),
    ("vg310_mgcp.csv", "VG310TEST",        "VG310 MGCP test",
     [(2, 0, 48)], 6000, "MGCP"),
    # VG350 — two-slot, 72-port FXS cards in slots 2 & 4 (matches the
    # real cluster's SKIGWEFBEEF9910 layout used during initial migration
    # debugging — SM-D-72FXS-SCCP × 2, 144 endpoints total).
    ("vg350_sccp.csv", "SKIGWEFBEEF3500", "VG350 SCCP test",
     [(2, 0, 72), (4, 0, 72)], 3000, "SCCP"),
    ("vg350_mgcp.csv", "VG350TEST",        "VG350 MGCP test",
     [(2, 0, 72), (4, 0, 72)], 7000, "MGCP"),
]

SCCP_HEADER = ("DOMAIN NAME,DESCRIPTION,SLOT,SUBUNIT,PORT NUMBER,"
                "PORT DESCRIPTION,PORT DIRECTORY NUMBER,CSS,"
                "ROUTE PARTITION,DISPLAY")
MGCP_HEADER = ("DOMAIN NAME,DESCRIPTION,SLOT,SUBUNIT,PORT NUMBER,"
                "PORT DESCRIPTION,PORT DIRECTORY NUMBER")


def gen_one(filename, domain, description, slots, dn_base, protocol):
    rows = [SCCP_HEADER if protocol == "SCCP" else MGCP_HEADER]
    filled = 0
    skipped = 0
    dn_offset = 0
    for slot, subunit, port_count in slots:
        for p in range(port_count):
            port_1based = p + 1
            if port_1based % 5 == 0:
                # Skip this port to leave a realistic gap, but still
                # advance the DN counter so DNs reflect the physical
                # position rather than shifting after each gap.
                dn_offset += 1
                skipped += 1
                continue
            dn = f"\\+1000998{dn_base + dn_offset:04d}"
            display     = f"Test {dn_base + dn_offset:04d}"
            port_desc   = f"{description} {slot}/{subunit}/{p}"
            if protocol == "SCCP":
                rows.append(
                    f"{domain},{description},{slot},{subunit},{p},"
                    f"{port_desc},{dn},,dn_PT,{display}"
                )
            else:
                rows.append(
                    f"{domain},{description},{slot},{subunit},{p},"
                    f"{port_desc},{dn}"
                )
            dn_offset += 1
            filled += 1
    path = os.path.join(OUT_DIR, filename)
    with open(path, "w") as f:
        f.write("\n".join(rows) + "\n")
    print(f"  {filename:20s}  {filled:3d} filled / {skipped:3d} skipped  "
          f"DNs +1000998{dn_base:04d}..+1000998{dn_base + dn_offset - 1:04d}")


def main():
    print(f"Writing {len(GATEWAYS)} CSVs to {OUT_DIR}")
    for spec in GATEWAYS:
        gen_one(*spec)
    print("Done.")


if __name__ == "__main__":
    main()
