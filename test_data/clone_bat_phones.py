#!/usr/bin/env python3
"""
Clone a BAT phone-template seed device across every port of a chassis.

Reads one already-built template device (e.g. ANvg224sccp-template400)
via AXL getPhone + getLine, then calls addPhone for every other chassis
port the layout calls for. Line pattern / label / display fields with
digits get the digit substituted with the new chassis-global port
number — so 'vg224 line 1 sccp' becomes 'vg224 line 2 sccp', etc.

Usage:
    python3 clone_bat_phones.py CLUSTER_ID SEED_DEVICE LAYOUT [--dry-run]

LAYOUT is comma-separated slot:subunit:port_count entries, sorted in
the physical card order you want chassis numbering to follow.

Examples:
    # VG224 — one 24-port card in slot 2
    python3 clone_bat_phones.py 3 ANvg224sccp-template400 2:0:24

    # VG310 — one 48-port card in slot 2
    python3 clone_bat_phones.py 3 ANvg310sccp-template400 2:0:48

    # VG350 — two 72-port cards (chassis numbering 1..72, 73..144)
    python3 clone_bat_phones.py 3 ANvg350sccp-template400 2:0:72,4:0:72

The seed's HHH suffix is excluded automatically. Re-run is safe with
--dry-run to preview what would be created.
"""

import argparse
import getpass
import os
import re
import sys
import uuid

# Import the project's existing AXL + DB helpers.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import axl       # noqa: E402
import database as db  # noqa: E402


def _new_pkid() -> str:
    """CUCM pkid format = a bare lowercase UUID (no braces)."""
    return str(uuid.uuid4())


# Informix `coltype` encoding: low 8 bits = type code, bit 0x100 (256) =
# NOT NULL flag. Type codes we care about: 1=SMALLINT 2=INTEGER 3=FLOAT
# 4=SMALLFLOAT 5=DECIMAL 6=SERIAL 7=DATE 8=MONEY 10=DATETIME 17=INT8
# 18=SERIAL8 — anything else is treated as a string for empty-value defaulting.
_INFORMIX_NUMERIC_TYPES = {1, 2, 3, 4, 5, 6, 7, 8, 10, 17, 18}


def _fetch_schema(creds: dict, table: str) -> dict[str, tuple[bool, int]]:
    """
    Return {colname_lower: (is_nullable, base_type)} for the given CUCM
    Informix table by querying syscolumns. Used to pick the right empty
    representation per column without manual allowlists.
    """
    rows = axl.raw_query(
        **creds,
        sql=(
            "SELECT LOWER(c.colname) AS colname, c.coltype "
            "FROM syscolumns c "
            "JOIN systables t ON t.tabid = c.tabid "
            f"WHERE t.tabname = '{table}'"
        ),
    )
    schema = {}
    for r in rows:
        col = (r.get("colname") or "").strip()
        try:
            ct = int(r.get("coltype") or 0)
        except (TypeError, ValueError):
            ct = 0
        not_null = bool(ct & 0x100)
        base    = ct & 0xFF
        schema[col] = (not not_null, base)
    return schema


def _sql_lit(v, col_name: str = "",
              schema: dict[str, tuple[bool, int]] | None = None) -> str:
    """
    Render a Python value as a SQL literal for Informix executeSQLUpdate.

    CUCM's executeSQLQuery collapses NULL and empty string to '' so we
    cannot distinguish them from a seed read. Resolve per column using
    the syscolumns schema we fetched upfront:
      * Real Python None → NULL.
      * '' on a nullable column → NULL (also dodges regex validators on
        columns like ECPublicKeyCurve where '' fails min-length).
      * '' on a NOT NULL numeric/date column → 0.
      * '' on a NOT NULL string column → ''.
      * Non-empty → escaped+quoted string literal.
    """
    if v is None:
        return "NULL"
    if v == "":
        if schema is not None:
            nullable, base_type = schema.get(col_name.lower(), (True, 0))
            if nullable:
                return "NULL"
            return "0" if base_type in _INFORMIX_NUMERIC_TYPES else "''"
        # Without schema, default to NULL — safer when we don't know.
        return "NULL"
    s = str(v)
    return "'" + s.replace("'", "''") + "'"


def _build_insert(table: str, row: dict, overrides: dict,
                   schema: dict[str, tuple[bool, int]] | None = None) -> str:
    """
    Build an INSERT statement from a row dict (as returned by axl.raw_query)
    with optional per-column overrides. Drops keys the override sets to the
    sentinel `...` so we can skip auto-managed columns. `schema` (from
    _fetch_schema) drives empty-value handling per column.
    """
    merged = {**row, **overrides}
    merged = {k: v for k, v in merged.items() if v is not ...}
    cols = ", ".join(merged.keys())
    vals = ", ".join(_sql_lit(v, k, schema) for k, v in merged.items())
    return f"INSERT INTO {table} ({cols}) VALUES ({vals})"


def _inspect_seed(creds: dict, seed_name: str) -> None:
    """
    Run SQL probes to find out what makes the seed device different from a
    normal phone. addPhone error 491 ("invalid characters or not formatted
    correctly for this device type") usually means the seed lives under a
    different tkclass/tkmodel than addPhone expects for that product, so
    cloning has to go through a different code path.
    """
    print(f"Inspecting seed device: {seed_name}\n")

    # 1. tkclass + tkmodel + tkproduct enums (translated to readable names)
    sql = (
        "SELECT d.name, d.description, d.tkclass, "
        "tc.name AS class_name, "
        "d.tkmodel, tm.name AS model_name, "
        "d.tkproduct, tp.name AS product_name, "
        "d.tkdeviceprotocol "
        "FROM device d "
        "LEFT JOIN typeclass tc      ON tc.enum = d.tkclass "
        "LEFT JOIN typemodel tm      ON tm.enum = d.tkmodel "
        "LEFT JOIN typeproduct tp    ON tp.enum = d.tkproduct "
        f"WHERE d.name = '{seed_name}'"
    )
    try:
        rows = axl.raw_query(**creds, sql=sql)
    except Exception as e:
        print(f"  [device row probe FAILED] {e}")
        rows = []
    if not rows:
        print(f"  No device row found for '{seed_name}'.")
    else:
        r = rows[0]
        print(f"  tkclass    = {r.get('tkclass')!r}  ({r.get('class_name')})")
        print(f"  tkmodel    = {r.get('tkmodel')!r}  ({r.get('model_name')})")
        print(f"  tkproduct  = {r.get('tkproduct')!r}  ({r.get('product_name')})")
        print(f"  protocol   = {r.get('tkdeviceprotocol')!r}")
        print(f"  description= {r.get('description')!r}")

    # 2. Find other devices on the same BAT template (if any) so we know
    #    what the "class" of BAT-template-phone-on-template actually is.
    print("\nOther devices that look like BAT-template entries (name LIKE 'AN%-template%'):")
    sql2 = (
        "SELECT d.name, tc.name AS class_name, tm.name AS model_name "
        "FROM device d "
        "LEFT JOIN typeclass tc ON tc.enum = d.tkclass "
        "LEFT JOIN typemodel  tm ON tm.enum = d.tkmodel "
        "WHERE d.name LIKE 'AN%-template%' "
        "ORDER BY d.name"
    )
    try:
        rows2 = axl.raw_query(**creds, sql=sql2)
    except Exception as e:
        print(f"  [probe FAILED] {e}")
        rows2 = []
    if rows2:
        for r in rows2[:10]:
            print(f"  {r.get('name'):40s}  class={r.get('class_name')}  model={r.get('model_name')}")
        if len(rows2) > 10:
            print(f"  ... and {len(rows2) - 10} more")
    else:
        print("  (none)")

    # 3. Distinct tkclass values in the device table so we can see what
    #    enums are valid for BAT.
    print("\nDistinct tkclass values currently in `device`:")
    try:
        rows3 = axl.raw_query(
            **creds,
            sql=("SELECT tc.enum, tc.name, COUNT(*) AS n "
                 "FROM device d JOIN typeclass tc ON tc.enum = d.tkclass "
                 "GROUP BY tc.enum, tc.name ORDER BY tc.enum"),
        )
        for r in rows3:
            print(f"  enum={r.get('enum')}  {r.get('name'):30s}  n={r.get('n')}")
    except Exception as e:
        print(f"  [probe FAILED] {e}")


def _load_creds(cluster_id: int) -> dict:
    """
    Return cluster credentials, prompting for the app's master password if
    the Fernet key isn't yet loaded in this process. The web app loads it
    at startup via launch.py; a fresh CLI run has to unlock on its own.
    """
    try:
        creds = db.get_cluster_credentials(cluster_id)
    except RuntimeError as e:
        if "locked" not in str(e).lower():
            raise
        print("Database is locked — enter your master password to unlock.")
        for _ in range(3):
            pw = getpass.getpass("  Master password: ")
            if db.unlock(pw):
                break
            print("  Incorrect password.")
        else:
            sys.exit("Too many failed attempts.")
        creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        sys.exit(f"Cluster id {cluster_id} not found in clusters.db.")
    return creds


# Fields whose digits should be substituted with the new chassis-global
# port number. Order matters only for the user-facing dry-run preview.
_DIGIT_FIELDS = ("pattern", "label", "display", "displayAscii",
                  "alertingName", "description")


def parse_layout(spec: str):
    out = []
    for chunk in spec.split(","):
        parts = chunk.strip().split(":")
        if len(parts) != 3:
            raise ValueError(f"bad layout chunk {chunk!r}; want slot:subunit:count")
        slot, sub, count = (int(parts[0]), int(parts[1]), int(parts[2]))
        if count < 1:
            raise ValueError(f"count must be >= 1 in {chunk!r}")
        out.append((slot, sub, count))
    return out


def hhh_hex(slot: int, subunit: int, port_0based: int) -> str:
    """3-uppercase-hex HHH suffix for an AN device name."""
    return f"{(slot << 9) | (subunit << 7) | port_0based:03X}"


_LINE_N_RE = re.compile(r"\bline\s+(\d+)", re.IGNORECASE)


def substitute_digit(s: str | None, new_n: int) -> str | None:
    """
    Substitute the per-port digit in a BAT-template string with `new_n`.

    Heuristics, in order:
      1. If the string contains a 'line N' run (case-insensitive), swap
         the N — handles the canonical BAT line-name convention.
      2. Otherwise swap the LAST digit run — survives model identifiers
         like 'vg224' that would otherwise be misinterpreted as the
         port number (which a 'first digit run' rule does).
      3. If there are no digits at all, append ' N' so each clone is
         still distinct.

    None and empty strings pass through unchanged.
    """
    if s is None or s == "":
        return s
    m = _LINE_N_RE.search(s)
    if m:
        return s[:m.start(1)] + str(new_n) + s[m.end(1):]
    matches = list(re.finditer(r"\d+", s))
    if matches:
        last = matches[-1]
        return s[:last.start()] + str(new_n) + s[last.end():]
    return f"{s} {new_n}"


def clone_line(line: dict, new_n: int) -> dict:
    out = dict(line)
    for field in _DIGIT_FIELDS:
        if field in out and out[field] is not None:
            out[field] = substitute_digit(out[field], new_n)
    return out


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("cluster_id", type=int,
                    help="Cluster row id from clusters.db (see Clusters tab)")
    p.add_argument("seed_device",
                    help="e.g. ANvg224sccp-template400")
    p.add_argument("layout",
                    help="slot:subunit:count[,slot:subunit:count,...]")
    p.add_argument("--dry-run", action="store_true",
                    help="Print plan without calling addPhone")
    p.add_argument("--inspect", action="store_true",
                    help="Run SQL probes against the seed to find out why "
                         "addPhone might be rejecting clones, then exit.")
    args = p.parse_args()

    creds = _load_creds(args.cluster_id)

    if args.inspect:
        _inspect_seed(creds, args.seed_device)
        return

    layout = parse_layout(args.layout)
    seed_name = args.seed_device
    seed_hhh = seed_name[-3:].upper()
    base = seed_name[:-3]
    if len(seed_hhh) != 3 or not re.fullmatch(r"[0-9A-F]{3}", seed_hhh):
        sys.exit(f"Seed device {seed_name!r} doesn't end in 3 hex chars "
                  f"(HHH suffix). Cannot clone.")

    # The seed is tkclass=253 (Phone Template), not tkclass=1 (Phone).
    # addPhone always creates tkclass=1 and runs a strict name validator
    # against AN<12-hex><HHH> for product=Analog Phone — which is why it
    # rejected 'ANvg224sccp-template4XX' clones.
    #
    # Workaround: read the seed's rows directly from device / numplan /
    # devicenumplanmap via executeSQLQuery, then INSERT verbatim copies
    # (with new pkid + new name + substituted pattern) via
    # executeSQLUpdate. CUCM's name validator only runs at the AXL phone
    # API boundary, not on direct DB inserts.
    seed_dev = axl.raw_query(
        **creds,
        sql=f"SELECT * FROM device WHERE name = '{seed_name}'",
    )
    if not seed_dev:
        sys.exit(f"Seed device {seed_name!r} not found in `device` table.")
    seed_row = seed_dev[0]
    seed_pkid = seed_row["pkid"]

    # Linked lines: pull devicenumplanmap rows for the seed, then pull the
    # numplan rows they reference. Separate queries so we can use SELECT *
    # on each table and not have to enumerate columns (which differ across
    # CUCM versions). The two row dicts are kept side-by-side in `seed_lines`.
    seed_map_rows = axl.raw_query(
        **creds,
        sql=f"SELECT * FROM devicenumplanmap WHERE fkdevice = '{seed_pkid}'",
    )
    # Chassis binding (links device to a slot/subunit/port on the parent
    # gateway/template). For SCCP analog the table is mgcpdevicemember, same
    # one bind_phone_to_gateway uses for real VG224 phones. BAT phone
    # templates of an analog gateway reuse the same binding scheme — without
    # one of these rows the device exists in the DB but doesn't appear on
    # the chassis page of the template/gateway.
    seed_mgcp_rows = axl.raw_query(
        **creds,
        sql=f"SELECT * FROM mgcpdevicemember WHERE fkdevice = '{seed_pkid}'",
    )
    mgcp_schema_lookup: dict[str, tuple[bool, int]] = {}
    if seed_mgcp_rows:
        mgcp_schema_lookup = _fetch_schema(creds, "mgcpdevicemember")

    seed_lines: list[dict] = []
    for map_row in seed_map_rows:
        np_pkid = map_row.get("fknumplan")
        if not np_pkid:
            continue
        np_rows = axl.raw_query(
            **creds,
            sql=f"SELECT * FROM numplan WHERE pkid = '{np_pkid}'",
        )
        if not np_rows:
            continue
        seed_lines.append({"map": map_row, "np": np_rows[0]})

    # Compute every chassis position the layout calls for and assign each
    # a 1-based chassis-global port number. Skip the seed's HHH so we
    # don't try to recreate it.
    targets = []
    chassis_n = 0
    seed_chassis_n = None
    for slot, sub, count in layout:
        for p_0 in range(count):
            chassis_n += 1
            h = hhh_hex(slot, sub, p_0)
            if h == seed_hhh:
                seed_chassis_n = chassis_n
                continue
            targets.append({
                "slot": slot, "subunit": sub, "port_0": p_0,
                "hhh": h, "chassis_n": chassis_n,
                "new_name": base + h,
            })
    if seed_chassis_n is None:
        sys.exit(f"Seed HHH {seed_hhh} doesn't appear in the layout "
                  f"{args.layout!r}. Double-check the slot/subunit/count.")

    # ----- preview -----
    print(f"Seed device: {seed_name}  (chassis port {seed_chassis_n})")
    print(f"  pkid       : {seed_pkid}")
    print(f"  tkclass    : {seed_row.get('tkclass')}  (253 = Phone Template)")
    print(f"  description: {seed_row.get('description')}")
    print(f"  device col#: {len(seed_row)}")
    print(f"Seed chassis bind ({len(seed_mgcp_rows)} mgcpdevicemember row(s)):")
    for mm in seed_mgcp_rows:
        print(f"  fkmgcp={mm.get('fkmgcp')}  slot={mm.get('slot')}  "
              f"subunit={mm.get('subunit')}  port={mm.get('port')}")
    if not seed_mgcp_rows:
        print("  (none — clones will be in the device table but won't show "
              "up on the chassis page)")
    print(f"Seed line(s) : {len(seed_lines)}")
    for ln in seed_lines:
        np  = ln["np"]
        mp  = ln["map"]
        print(f"  pattern={np.get('dnorpattern')!r:<30} "
              f"label={mp.get('label')!r:<25} "
              f"fkroutepartition={np.get('fkroutepartition')!r}")
    print(f"\nWill clone {len(targets)} device(s) (skipping seed at {seed_hhh}).")
    print("Sample first 3 plan rows:")
    for t in targets[:3]:
        if seed_lines:
            preview_pattern = substitute_digit(
                seed_lines[0]["np"].get("dnorpattern"), t["chassis_n"])
        else:
            preview_pattern = "(no seed line)"
        print(f"  {t['new_name']}  S{t['slot']}/SU{t['subunit']}/{t['port_0']+1}"
              f"  chassis#{t['chassis_n']:>3}  pattern={preview_pattern!r}")
    if len(targets) > 3:
        print(f"  ... and {len(targets) - 3} more")

    if args.dry_run:
        print("\n--dry-run set, nothing was created.")
        return

    # CUCM's `device` table has a unique-not-null `ctiid` (integer used for
    # CTI lookups). Copying the seed's ctiid verbatim violates the unique
    # constraint, so allocate fresh ctiids starting from MAX(ctiid)+1.
    ctiid_rows = axl.raw_query(**creds,
                                 sql="SELECT MAX(ctiid) AS max_id FROM device")
    try:
        next_ctiid = int(ctiid_rows[0].get("max_id") or 0) + 1
    except (TypeError, ValueError):
        next_ctiid = 100000  # fall back to a large unlikely-to-collide value

    # devicenumplanmap also has a unique not-null ctiid, separately scoped
    # from the device table's. Same allocation strategy.
    dnp_rows = axl.raw_query(
        **creds,
        sql="SELECT MAX(ctiid) AS max_id FROM devicenumplanmap")
    try:
        next_dnp_ctiid = int(dnp_rows[0].get("max_id") or 0) + 1
    except (TypeError, ValueError):
        next_dnp_ctiid = 100000

    # Schema lookups so empty values get rendered as NULL / 0 / '' per the
    # actual column metadata instead of guessed allowlists.
    print("Fetching schemas for device / numplan / devicenumplanmap …")
    device_schema  = _fetch_schema(creds, "device")
    numplan_schema = _fetch_schema(creds, "numplan")
    dnpmap_schema  = _fetch_schema(creds, "devicenumplanmap")
    print(f"  device           : {len(device_schema)} cols  "
          f"({sum(1 for n, _ in device_schema.values() if not n)} NOT NULL)")
    print(f"  numplan          : {len(numplan_schema)} cols  "
          f"({sum(1 for n, _ in numplan_schema.values() if not n)} NOT NULL)")
    print(f"  devicenumplanmap : {len(dnpmap_schema)} cols  "
          f"({sum(1 for n, _ in dnpmap_schema.values() if not n)} NOT NULL)")

    # Pre-clean: drop any prior clones that share our naming pattern so the
    # script is idempotent — re-run it any number of times and the end
    # state is identical (seed + N cloned ports). Restricted to tkclass=253
    # (Phone Template) as a safety so we never touch real registered phones.
    base_esc = base.replace("'", "''")
    seed_esc = seed_name.replace("'", "''")
    existing_clones = axl.raw_query(
        **creds,
        sql=(
            f"SELECT pkid, name FROM device "
            f"WHERE name LIKE '{base_esc}%' "
            f"AND name != '{seed_esc}' "
            f"AND tkclass = 253"
        ),
    )
    if existing_clones:
        print(f"\nFound {len(existing_clones)} existing clone(s); cleaning up "
               f"before re-creating:")
        for ex in existing_clones:
            ex_name = ex.get("name") or ""
            ex_pkid = ex.get("pkid") or ""
            print(f"  - {ex_name}")
            # Capture numplan rows linked to this device so we can also
            # drop any that end up orphaned (shared lines with no other
            # device referencing them stay; private lines get cleaned up).
            np_rows = axl.raw_query(
                **creds,
                sql=("SELECT fknumplan FROM devicenumplanmap "
                     f"WHERE fkdevice = '{ex_pkid}'"),
            )
            np_pkids = [r.get("fknumplan") for r in np_rows if r.get("fknumplan")]
            # Delete in FK dependency order: bind → map → device → orphan np.
            for sql in (
                f"DELETE FROM mgcpdevicemember WHERE fkdevice = '{ex_pkid}'",
                f"DELETE FROM devicenumplanmap WHERE fkdevice = '{ex_pkid}'",
                f"DELETE FROM device           WHERE pkid     = '{ex_pkid}'",
            ):
                try:
                    axl.execute_sql_update(**creds, sql=sql)
                except Exception as e:
                    print(f"      step failed (continuing): {e}")
            for npp in np_pkids:
                refs = axl.raw_query(
                    **creds,
                    sql=("SELECT COUNT(*) AS n FROM devicenumplanmap "
                         f"WHERE fknumplan = '{npp}'"),
                )
                try:
                    n_left = int(refs[0].get("n") or 0) if refs else 0
                except (TypeError, ValueError):
                    n_left = 0
                if n_left == 0:
                    try:
                        axl.execute_sql_update(
                            **creds,
                            sql=f"DELETE FROM numplan WHERE pkid = '{npp}'",
                        )
                    except Exception:
                        pass

    # Compute the line patterns we plan to insert so we can detect (and
    # delete) orphan numplan rows from previous failed runs. Each failed
    # iteration before today's rollback fix left a numplan row behind
    # because we only deleted the device row, and numplan's (pattern,
    # partition) unique constraint now blocks our new INSERTs.
    planned_patterns: set[str] = set()
    for t in targets:
        for ln in seed_lines:
            np_pat = substitute_digit(ln["np"].get("dnorpattern"),
                                       t["chassis_n"])
            if np_pat:
                planned_patterns.add(np_pat)
    if planned_patterns:
        in_clause = ",".join("'" + p.replace("'", "''") + "'"
                              for p in planned_patterns)
        orphans = axl.raw_query(
            **creds,
            sql=(
                "SELECT np.pkid, np.dnorpattern "
                "FROM numplan np "
                f"WHERE np.dnorpattern IN ({in_clause}) "
                "AND NOT EXISTS (SELECT 1 FROM devicenumplanmap dnp "
                "                 WHERE dnp.fknumplan = np.pkid)"
            ),
        )
        if orphans:
            print(f"\nFound {len(orphans)} orphan numplan row(s) from prior "
                   f"failed run(s); cleaning up before clone:")
            for r in orphans:
                pat = r.get("dnorpattern")
                pkid = r.get("pkid")
                print(f"  - {pat!r} ({pkid})")
                try:
                    axl.execute_sql_update(
                        **creds,
                        sql=f"DELETE FROM numplan WHERE pkid = '{pkid}'",
                    )
                except Exception as e:
                    print(f"      delete failed: {e}")

    # ----- clone via SQL -----
    ok = 0
    failed: list[tuple[str, str]] = []
    print()
    for t in targets:
        new_dev_pkid = _new_pkid()
        # 1. device row
        try:
            sql = _build_insert("device", seed_row, overrides={
                "pkid":        new_dev_pkid,
                "name":        t["new_name"],
                "description": t["new_name"],
                "ctiid":       str(next_ctiid),
            }, schema=device_schema)
            axl.execute_sql_update(**creds, sql=sql)
            next_ctiid += 1
        except Exception as e:
            print(f"  ! {t['new_name']}  device INSERT failed: {e}")
            failed.append((t["new_name"], f"device: {e}"))
            continue

        # 2. lines (numplan + devicenumplanmap, one pair per seed line)
        line_failed = None
        inserted_np_pkids: list[str] = []
        for ln in seed_lines:
            np_row  = ln["np"]
            map_row = ln["map"]
            new_np_pkid  = _new_pkid()
            new_map_pkid = _new_pkid()
            new_pattern = substitute_digit(np_row.get("dnorpattern"),
                                            t["chassis_n"])
            new_label   = substitute_digit(map_row.get("label"),
                                            t["chassis_n"])
            new_display = substitute_digit(map_row.get("display"),
                                            t["chassis_n"])
            new_da      = substitute_digit(map_row.get("displayascii"),
                                            t["chassis_n"])
            try:
                axl.execute_sql_update(
                    **creds,
                    sql=_build_insert("numplan", np_row, overrides={
                        "pkid":        new_np_pkid,
                        "dnorpattern": new_pattern,
                    }, schema=numplan_schema),
                )
                inserted_np_pkids.append(new_np_pkid)
                axl.execute_sql_update(
                    **creds,
                    sql=_build_insert("devicenumplanmap", map_row, overrides={
                        "pkid":         new_map_pkid,
                        "fkdevice":     new_dev_pkid,
                        "fknumplan":    new_np_pkid,
                        "ctiid":        str(next_dnp_ctiid),
                        "label":        new_label,
                        "display":      new_display,
                        "displayascii": new_da,
                    }, schema=dnpmap_schema),
                )
                next_dnp_ctiid += 1
            except Exception as e:
                line_failed = str(e)
                # Roll back inserted numplan rows + the device row so a
                # re-run starts from a clean state. Unmapped numplan rows
                # otherwise survive and trigger the (pattern, partition)
                # unique constraint on the next attempt.
                for np_pkid in inserted_np_pkids:
                    try:
                        axl.execute_sql_update(
                            **creds,
                            sql=f"DELETE FROM numplan WHERE pkid = '{np_pkid}'",
                        )
                    except Exception:
                        pass
                try:
                    axl.execute_sql_update(
                        **creds,
                        sql=f"DELETE FROM device WHERE pkid = '{new_dev_pkid}'",
                    )
                except Exception:
                    pass
                break

        if line_failed:
            print(f"  ! {t['new_name']}  line INSERT failed (device rolled back): {line_failed}")
            failed.append((t["new_name"], f"line: {line_failed}"))
            continue

        # 3. chassis binding via mgcpdevicemember — without this, the
        #    device exists in the database but doesn't appear on the
        #    template/gateway's chassis page in the GUI.
        bind_failed = None
        for mm_row in seed_mgcp_rows:
            new_mm_pkid = _new_pkid()
            try:
                axl.execute_sql_update(
                    **creds,
                    sql=_build_insert("mgcpdevicemember", mm_row, overrides={
                        "pkid":     new_mm_pkid,
                        "fkdevice": new_dev_pkid,
                        "slot":     str(t["slot"]),
                        "subunit":  str(t["subunit"]),
                        "port":     str(t["port_0"]),
                    }, schema=mgcp_schema_lookup),
                )
            except Exception as e:
                bind_failed = str(e)
                break

        if bind_failed:
            # Roll back lines + device so a re-run is clean.
            for np_pkid in inserted_np_pkids:
                try:
                    axl.execute_sql_update(**creds,
                        sql=f"DELETE FROM numplan WHERE pkid = '{np_pkid}'")
                except Exception:
                    pass
            try:
                axl.execute_sql_update(**creds,
                    sql=f"DELETE FROM device WHERE pkid = '{new_dev_pkid}'")
            except Exception:
                pass
            print(f"  ! {t['new_name']}  mgcpdevicemember INSERT failed: {bind_failed}")
            failed.append((t["new_name"], f"bind: {bind_failed}"))
            continue

        pat = substitute_digit(seed_lines[0]["np"].get("dnorpattern"),
                                t["chassis_n"]) if seed_lines else "(no line)"
        print(f"  + {t['new_name']}  line={pat!r}")
        ok += 1

    print(f"\nDone: {ok} created, {len(failed)} failed.")
    if failed:
        print("\nFailed devices (first 5):")
        for name, err in failed[:5]:
            print(f"  {name}: {err}")


if __name__ == "__main__":
    main()
