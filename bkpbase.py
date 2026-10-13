#!/usr/bin/env python
"""Give a restorable-full verdict over exported msdb.backupset rows.

The gate everybody writes first asks msdb for the newest full backup:

    SELECT TOP (1) ... FROM msdb.dbo.backupset
    WHERE database_name = ? AND type = 'D'
    ORDER BY backup_finish_date DESC;

A storage snapshot is also a type D row. If the newest full is a snapshot, the
gate passes and there is no backup file to restore. Filtering on is_copy_only
does not fix it, because a snapshot can be a regular full. Filtering on the
device type does not fix it either, because a T-SQL snapshot backup writes its
metadata file TO DISK.

This reads the rows that query would read, as a CSV, and walks back from the
newest full until it finds one that a plain RESTORE DATABASE could use. It
refuses snapshot rows, copy-only rows, damaged rows, rows on a virtual device,
and rows whose file a later backup started again at position 1. Then it checks
the age of the full it found.

    python bkpbase.py --self-test
    python bkpbase.py --query > export.sql
    python bkpbase.py history.csv --database appdb
    python bkpbase.py history.csv --database appdb --max-age-hours 26 --strict
    python bkpbase.py history.csv --out verdict.json --apply

Exit codes: 0 every database passed, 1 a database failed, 2 a file could not
be read or written, 64 usage error. Nothing is written without --apply.
"""

from __future__ import print_function

import argparse
import csv
import datetime
import importlib.util
import io
import json
import os
import re
import sys
import tempfile

# =============================================================================
# CONFIGURATION
# =============================================================================

# The newest restorable full may be at most this old. 26 hours fits a daily
# full with two hours of slack for a run that starts late.
DEFAULT_MAX_AGE_HOURS = 26.0

# backupmediafamily.device_type values. Only disk and URL hold the backup in a
# file that RESTORE DATABASE reads directly.
NATIVE_DEVICES = (2, 9)
DEVICE_NAMES = {2: "disk", 5: "tape", 7: "virtual device",
                9: "Azure Storage", 105: "permanent backup device"}

RECOVERY_MODELS = ("FULL", "BULK-LOGGED", "SIMPLE")

# The columns the export must hold. EXPORT_SQL produces exactly these.
COLUMNS = ("backup_set_id", "server_name", "database_name", "type",
           "is_copy_only", "is_snapshot", "is_damaged", "recovery_model",
           "backup_finish_date", "position", "device_type",
           "physical_device_name")

# LEFT JOIN so that a set with no media family row still arrives, with a blank
# device type, and is refused. Bits are cast so that every client exports 0/1,
# and the date uses style 121 so that no client applies its own locale.
EXPORT_SQL = """\
SELECT bs.backup_set_id,
       bs.server_name,
       bs.database_name,
       bs.type,
       CAST(bs.is_copy_only AS tinyint) AS is_copy_only,
       CAST(bs.is_snapshot AS tinyint) AS is_snapshot,
       CAST(bs.is_damaged AS tinyint) AS is_damaged,
       bs.recovery_model,
       CONVERT(varchar(23), bs.backup_finish_date, 121) AS backup_finish_date,
       bs.position,
       bmf.device_type,
       bmf.physical_device_name
FROM msdb.dbo.backupset AS bs
LEFT JOIN msdb.dbo.backupmediafamily AS bmf
       ON bmf.media_set_id = bs.media_set_id
WHERE bs.backup_finish_date >= DATEADD(DAY, -14, GETDATE())
ORDER BY bs.backup_set_id;
"""

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_READ = 2
EXIT_USAGE = 64


class ColumnError(ValueError):
    """The export lacks a required column. A usage error."""


class ExportError(ValueError):
    """The export cannot be read as backup history."""


# ======================================================================= parse

_WHEN = re.compile(r"^\s*([0-9]{4})-([0-9]{2})-([0-9]{2})[ T]([0-9]{2}):([0-9]{2})"
                   r"(?::([0-9]{2})(?:\.([0-9]{1,7}))?)?\s*$")
# At most 18 digits: a longer string would make int() raise on Python 3.11+, and
# no msdb id, position or device type comes near it.
_INT = re.compile(r"^\s*-?[0-9]{1,18}\s*$")
_BITS = {"1": True, "true": True, "0": False, "false": False}


def parse_when(text):
    """yyyy-mm-dd hh:mi[:ss[.fff]] to a naive datetime, or None."""
    m = _WHEN.match(text)
    if not m:
        return None
    y, mo, d, h, mi, s, frac = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    try:
        return datetime.datetime(int(y), int(mo), int(d), int(h), int(mi),
                                 int(s or 0), micro)
    except ValueError:
        return None


def _short(text):
    """A cell for an error message, cut so that a huge cell stays readable."""
    return repr(text if len(text) <= 60 else text[:57] + "...")


def parse_int(text):
    return int(text) if _INT.match(text) else None


def parse_bit(text):
    """0/1 or False/True to a bool, or None for anything else."""
    return _BITS.get(text.strip().lower())


def _new_set(set_id, row):
    problems = []

    def bit(col):
        value = parse_bit(row[col])
        if value is None:
            problems.append("%s is %s, not 0 or 1" % (col, _short(row[col])))
        return value

    finish = parse_when(row["backup_finish_date"])
    if finish is None:
        problems.append("backup_finish_date %s is not yyyy-mm-dd hh:mi:ss"
                        % _short(row["backup_finish_date"]))
    server = row["server_name"].strip()
    database = row["database_name"].strip()
    if not server:
        problems.append("server_name is blank")
    if not database:
        problems.append("database_name is blank")
    position = parse_int(row["position"])
    if position is None:
        problems.append("position %s is not an integer"
                        % _short(row["position"]))
    return {
        "id": set_id,
        "server": server,
        "database": database,
        "type": row["type"].strip().upper(),
        "copy_only": bit("is_copy_only"),
        "snapshot": bit("is_snapshot"),
        "damaged": bit("is_damaged"),
        "recovery": row["recovery_model"].strip().upper(),
        "finish": finish,
        "position": position,
        "devices": [],
        "files": [],
        "problems": problems,
    }


def read_export(text):
    """CSV text to a list of backup sets, ascending backup_set_id.

    The export joins backupset to backupmediafamily, so a striped or mirrored
    set arrives as one row per family. Rows that share a backup_set_id are one
    set, with every device type and file name they carry. backup_set_id is
    unique only inside one msdb, so a set is keyed on (server_name, id).
    """
    reader = csv.DictReader(io.StringIO(text), restval="")
    sets = {}
    try:
        # reading fieldnames reads line 1, so it sits inside the try
        reader.fieldnames = [(h or "").strip().lower()
                             for h in (reader.fieldnames or [])]
        missing = [c for c in COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ColumnError("the export lacks column(s): %s. Run --query "
                              "for the export this tool reads."
                              % ", ".join(missing))
        for row in reader:
            set_id = parse_int(row["backup_set_id"])
            if set_id is None:
                raise ExportError("line %d: backup_set_id %s is not an integer"
                                  % (reader.line_num,
                                     _short(row["backup_set_id"])))
            key = (row["server_name"].strip(), set_id)
            bs = sets.get(key)
            if bs is None:
                bs = sets[key] = _new_set(set_id, row)
            bs["devices"].append(parse_int(row["device_type"]))
            name = row["physical_device_name"].strip()
            if name and name not in bs["files"]:
                bs["files"].append(name)
    except csv.Error as exc:
        raise ExportError("line %d: %s" % (reader.line_num, exc))
    return [sets[k] for k in sorted(sets, key=lambda k: (k[1], k[0]))]


# ==================================================================== decide

def _key(bs):
    return (bs["server"], bs["id"])


def overwrites(sets):
    """Which sets are gone because a later backup started their file again.

    A backup set at position 1 is the first set in its file. Either INIT or
    FORMAT overwrote the file, or the file was deleted and made new. Both ways,
    every earlier set in that file is gone. A NOINIT backup appends at
    position 2 or later and removes nothing. A later writer with no position
    counts as a restart: unknown is not proof that the earlier set survived.

    Returns (lost, rewrote). Both are keyed on (server_name, backup_set_id):
    lost maps a lost set to the id that started its file again, and rewrote
    maps a set to how many sets it replaced.
    """
    in_file = {}
    lost = {}
    rewrote = {}
    for bs in sets:
        for name in bs["files"]:
            # shortcut: paths compare case-insensitively, which suits Windows;
            # on Linux two names differing only by case read as one file, so
            # the error is a refusal, never a pass.
            earlier = in_file.setdefault((bs["server"], name.lower()), [])
            if bs["position"] is None or bs["position"] <= 1:
                for old in earlier:
                    lost.setdefault(old, bs["id"])
                rewrote[_key(bs)] = rewrote.get(_key(bs), 0) + len(earlier)
                del earlier[:]
            earlier.append(_key(bs))
    return lost, rewrote


def refusals(bs, lost, as_of, allow_copy_only=False):
    """Every reason this full cannot be the restorable base. Empty means usable."""
    out = list(bs["problems"])
    if bs["snapshot"]:
        out.append("snapshot: the data is in a storage snapshot, not in the "
                   "backup file")
    if bs["copy_only"] and not allow_copy_only:
        out.append("copy-only: it cannot serve as a differential base")
    for dev in sorted(set(bs["devices"]), key=str):
        if dev not in NATIVE_DEVICES:
            out.append("device type %s (%s): not a disk or URL backup file"
                       % ("blank" if dev is None else dev,
                          DEVICE_NAMES.get(dev, "unknown")))
    if not bs["files"]:
        out.append("no backup file name is recorded")
    if bs["damaged"]:
        out.append("damaged: damage was detected and the backup was told to "
                   "continue despite errors")
    if _key(bs) in lost:
        out.append("overwritten: backup set %d later started its file again "
                   "at position 1" % lost[_key(bs)])
    if bs["finish"] is not None and bs["finish"] > as_of:
        out.append("finishes after the as-of time %s" % _fmt(as_of))
    return out


def _verdict(server, database):
    return {"server": server, "database": database, "status": "FAIL",
            "code": "NO-ROWS", "chosen": None, "age_hours": None,
            "skipped": [], "warnings": []}


def judge(server, database, sets, lost, rewrote, as_of,
          max_age_hours=DEFAULT_MAX_AGE_HOURS, allow_copy_only=False):
    """The verdict for one database: the newest full that is restorable."""
    v = _verdict(server, database)
    v["code"] = "NO-FULL"
    for bs in reversed([s for s in sets if s["type"] == "D"]):
        why = refusals(bs, lost, as_of, allow_copy_only)
        if why:
            v["skipped"].append((bs, why))
            continue
        v["chosen"] = bs
        v["age_hours"] = (as_of - bs["finish"]).total_seconds() / 3600.0
        if v["age_hours"] > max_age_hours:
            v["code"] = "STALE"
            return v
        v["code"] = "OK"
        v["status"] = "PASS"
        if bs["recovery"] == "SIMPLE":
            v["warnings"].append(
                "taken under SIMPLE recovery: it starts no log chain, so a "
                "restore stops at this full unless a later full or "
                "differential exists")
        elif bs["recovery"] not in RECOVERY_MODELS:
            v["warnings"].append("recovery_model %s is not FULL, BULK-LOGGED "
                                 "or SIMPLE" % _short(bs["recovery"]))
        if bs["copy_only"]:
            v["warnings"].append(
                "copy-only, allowed by --allow-copy-only: a differential taken "
                "after it is based on an older full")
        if bs["position"] > 1:
            v["warnings"].append(
                "the set is at position %d in its file, and a plain RESTORE "
                "reads position 1: restore it with FILE = %d"
                % (bs["position"], bs["position"]))
        if rewrote.get(_key(bs)):
            v["warnings"].append(
                "this backup started its file again at position 1, so the %d "
                "earlier set(s) history shows in that file are gone; a fixed "
                "file name keeps one full, and the next INIT run to that name "
                "overwrites this one" % rewrote[_key(bs)])
        if v["warnings"]:
            v["status"] = "WARN"
        return v
    return v


def decide(sets, as_of, max_age_hours=DEFAULT_MAX_AGE_HOURS, databases=None,
           server=None, allow_copy_only=False):
    """Verdicts for every (server, database) asked for, or every one present.

    The overwrite check runs over the whole export before any filter, because
    a backup of another database to the same file destroys this one too.
    """
    if not 0 < max_age_hours < float("inf"):
        raise ValueError("the maximum age must be above zero and finite")
    lost, rewrote = overwrites(sets)
    if server is not None:
        sets = [s for s in sets if s["server"] == server]
    keys = sorted(set((s["server"], s["database"]) for s in sets
                      if databases is None or s["database"] in databases))
    verdicts = [judge(k[0], k[1],
                      [s for s in sets if (s["server"], s["database"]) == k],
                      lost, rewrote, as_of, max_age_hours, allow_copy_only)
                for k in keys]
    for name in databases or []:
        if not any(v["database"] == name for v in verdicts):
            verdicts.append(_verdict(server or "", name))
    if not verdicts:
        verdicts.append(_verdict(server or "", ""))
    return verdicts


def gate(verdicts, strict=False):
    """Exit code for a list of verdicts."""
    bad = ("FAIL", "WARN") if strict else ("FAIL",)
    return EXIT_FAIL if any(v["status"] in bad for v in verdicts) else EXIT_PASS


# ==================================================================== report

def _fmt(when):
    return "unreadable date" if when is None else when.strftime("%Y-%m-%d %H:%M:%S")


def _label(v):
    if v["server"]:
        return "%s / %s" % (v["server"], v["database"])
    return v["database"]


def describe(v, max_age_hours=DEFAULT_MAX_AGE_HOURS):
    """Text lines for one verdict."""
    bs = v["chosen"]
    if v["code"] == "NO-ROWS":
        if not v["database"]:
            return ["FAIL  the export holds no backup history rows"]
        return ["FAIL  %s: no backup history rows for this database"
                % _label(v)]
    if v["code"] == "NO-FULL":
        lines = ["FAIL  %s: no restorable full backup in the export"
                 % _label(v)]
    elif v["code"] == "STALE":
        lines = ["FAIL  %s: the newest restorable full, set %d, finished %s, "
                 "%.1f h before the as-of time; the limit is %g h"
                 % (_label(v), bs["id"], _fmt(bs["finish"]), v["age_hours"],
                    max_age_hours)]
    else:
        lines = ["%-4s  %s: full set %d finished %s, %.1f h before the as-of "
                 "time" % (v["status"], _label(v), bs["id"], _fmt(bs["finish"]),
                           v["age_hours"])]
        lines.extend("      file: %s" % f for f in bs["files"])
        lines.extend("      warning: %s" % w for w in v["warnings"])
    if v["skipped"]:
        lines.append("      passed over %d newer full set(s):"
                     % len(v["skipped"]) if bs else
                     "      refused %d full set(s):" % len(v["skipped"]))
        for old, why in v["skipped"]:
            lines.append("        set %d  %s  %s"
                         % (old["id"], _fmt(old["finish"]), "; ".join(why)))
    return lines


def to_record(v):
    """A verdict as plain JSON data."""
    bs = v["chosen"]
    return {
        "server": v["server"],
        "database": v["database"],
        "status": v["status"],
        "code": v["code"],
        "backup_set_id": bs["id"] if bs else None,
        "backup_finish_date": _fmt(bs["finish"]) if bs else None,
        "position": bs["position"] if bs else None,
        "files": list(bs["files"]) if bs else [],
        "age_hours": None if v["age_hours"] is None else round(v["age_hours"], 2),
        "warnings": list(v["warnings"]),
        "refused": [{"backup_set_id": old["id"], "reasons": why}
                    for old, why in v["skipped"]],
    }


# ================================================================= self-test

def _harness():
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    return check, raises, passed, failed


def _row(set_id, db="appdb", btype="D", copy="0", snap="0", dev="2", path=None,
         finish="2026-10-06 01:00:00.000", pos="1", rec="FULL", dmg="0",
         server="srv-a"):
    if path is None:
        path = "D:\\bak\\%s_%d.bak" % (db, set_id)
    return {"backup_set_id": str(set_id), "server_name": server,
            "database_name": db, "type": btype, "is_copy_only": copy,
            "is_snapshot": snap, "is_damaged": dmg, "recovery_model": rec,
            "backup_finish_date": finish, "position": pos,
            "device_type": dev, "physical_device_name": path}


def _csv(rows, header=COLUMNS):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow([r.get(c, "") for c in COLUMNS])
    return buf.getvalue()


def _run(rows, **kw):
    as_of = kw.pop("as_of", AS_OF)
    return decide(read_export(_csv(rows)), as_of, **kw)


def _cli(argv):
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = main(argv)
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue(), err.getvalue()


AS_OF = datetime.datetime(2026, 10, 6, 20, 0, 0)


def self_test():
    """Assertions over the decision core and the CLI. No server, no network."""
    check, raises, passed, failed = _harness()
    print("bkpbase self-test: no server, no network, synthetic history only")
    print("-" * 68)

    # ---- the disaster: the newest type D row is a snapshot
    good = _row(10, finish="2026-10-06 01:00:00.000")
    snap = _row(12, snap="1", dev="7", path="{5f1c2a9e-0000-4000-8000-00000000000a}",
                finish="2026-10-06 19:00:00.000")
    log = _row(11, btype="L", path="D:\\bak\\appdb_11.trn",
               finish="2026-10-06 02:00:00.000")
    v = _run([good, log, snap])[0]
    check(v["status"] == "PASS" and v["chosen"]["id"] == 10,
          "a newer non-copy-only snapshot full is passed over, never chosen  "
          "<-- pinned defect")
    check(v["skipped"][0][0]["id"] == 12,
          "and the snapshot is listed as the newer row it refused")
    check(any(r.startswith("snapshot:") for r in v["skipped"][0][1]),
          "and the reason names it a snapshot")
    check(any("device type 7 (virtual device)" in r for r in v["skipped"][0][1]),
          "and names the virtual device it was written to")
    check(abs(v["age_hours"] - 19.0) < 1e-9,
          "the chosen full is 19.0 h old at the as-of time")
    v = _run([snap])[0]
    check(v["status"] == "FAIL" and v["code"] == "NO-FULL",
          "a history of snapshot fulls only is FAIL, not PASS  <-- pinned defect")
    snapcopy = _row(13, snap="1", copy="1", dev="7", path="{guid-b}",
                    finish="2026-10-06 19:30:00.000")
    v = _run([good, snapcopy])[0]
    check(v["status"] == "PASS" and v["chosen"]["id"] == 10,
          "a newer copy-only snapshot does not block an older good full  "
          "<-- pinned defect")
    bkm = _row(14, snap="1", dev="2", path="D:\\bak\\appdb.bkm",
               finish="2026-10-06 19:00:00.000")
    v = _run([good, bkm])[0]
    check(v["chosen"]["id"] == 10 and len(v["skipped"][0][1]) == 1,
          "a snapshot whose metadata file went TO DISK is still refused  "
          "<-- pinned defect")

    # ---- copy-only
    copy = _row(15, copy="1", finish="2026-10-06 10:00:00.000")
    v = _run([good, copy])[0]
    check(v["chosen"]["id"] == 10,
          "a copy-only full is refused as the base by default")
    check("differential base" in v["skipped"][0][1][0],
          "and the reason says it cannot be a differential base")
    v = _run([copy])[0]
    check(v["code"] == "NO-FULL", "a copy-only full alone is FAIL by default")
    v = _run([good, copy], allow_copy_only=True)[0]
    check(v["chosen"]["id"] == 15 and v["status"] == "WARN",
          "--allow-copy-only accepts it, as a WARN")
    check("older full" in v["warnings"][0],
          "and the warning says later differentials belong to an older full")

    # ---- device and file
    v = _run([_row(16, dev="5")])[0]
    check(v["code"] == "NO-FULL" and "(tape)" in v["skipped"][0][1][0],
          "a tape full is refused and named as tape")
    v = _run([_row(16, dev="9", path="https://acct.example/c/appdb.bak")])[0]
    check(v["status"] == "PASS", "a URL full is restorable")
    v = _run([_row(16, dev="", path="")])[0]
    check(v["code"] == "NO-FULL",
          "a set with no media family row (LEFT JOIN blank) is refused  "
          "<-- pinned defect")
    check(v["skipped"][0][1] == ["device type blank (unknown): not a disk or "
                                 "URL backup file",
                                 "no backup file name is recorded"],
          "and both the blank device and the missing file are named")
    v = _run([_row(16, dev="105")])[0]
    check("permanent backup device" in v["skipped"][0][1][0],
          "a permanent backup device (105) is refused and named")
    v = _run([_row(16, dev="2", path="D:\\bak\\s1.bak"),
              _row(16, dev="7", path="{guid-c}")])[0]
    check(v["code"] == "NO-FULL",
          "a striped set with one family on a virtual device is refused")
    v = _run([_row(17, path="D:\\bak\\s1.bak"), _row(17, path="D:\\bak\\s2.bak"),
              _row(17, path="D:\\bak\\s2.bak")])[0]
    check(v["chosen"]["files"] == ["D:\\bak\\s1.bak", "D:\\bak\\s2.bak"],
          "a striped set keeps each file once, in export order")

    # ---- damage and dates
    v = _run([_row(18, dmg="1")])[0]
    check(v["code"] == "NO-FULL" and v["skipped"][0][1][0].startswith("damaged"),
          "a damaged full is refused")
    v = _run([_row(18, finish="2026-10-07 01:00:00.000")])[0]
    check(v["code"] == "NO-FULL" and "after the as-of" in v["skipped"][0][1][0],
          "a full that finishes after the as-of time is refused")
    v = _run([_row(18, finish="10/6/2026 1:00:00 AM")])[0]
    check(v["code"] == "NO-FULL" and "not yyyy-mm-dd" in v["skipped"][0][1][0],
          "a locale date is refused, never guessed  <-- pinned defect")
    v = _run([_row(18, snap="yes")])[0]
    check(v["code"] == "NO-FULL" and "is_snapshot is 'yes'" in v["skipped"][0][1][0],
          "an unreadable is_snapshot refuses the row, never reads as 0  "
          "<-- pinned defect")
    v = _run([_row(18, snap="False", copy="FALSE", dmg="false")])[0]
    check(v["status"] == "PASS", "True/False bits from a PowerShell export read")
    v = _run([_row(18, finish="2026-10-06 01:00:00.000")],
             max_age_hours=19.0)[0]
    check(v["status"] == "PASS", "exactly the maximum age passes: the limit is "
                                 "inclusive")
    v = _run([_row(18, finish="2026-10-06 00:59:59.000")],
             max_age_hours=19.0)[0]
    check(v["code"] == "STALE" and v["status"] == "FAIL",
          "one second past the maximum age is STALE")
    v = _run([_row(18, finish="2026-10-04 01:00:00"), _row(19, snap="1")])[0]
    check(v["code"] == "STALE" and v["chosen"]["id"] == 18,
          "a fresh snapshot cannot hide a stale real full  <-- pinned defect")
    raises(lambda: _run([good], max_age_hours=0),
           "a maximum age of zero raises")
    raises(lambda: _run([good], max_age_hours=float("nan")),
           "a NaN maximum age raises")
    check(parse_when("2026-10-06 01:02:03.000") ==
          datetime.datetime(2026, 10, 6, 1, 2, 3),
          "a style 121 date parses")
    check(parse_when("2026-10-06T01:02:03.1234567") ==
          datetime.datetime(2026, 10, 6, 1, 2, 3, 123456),
          "an ISO date with seven fraction digits parses to microseconds")
    check(parse_when("2026-10-06 01:02") ==
          datetime.datetime(2026, 10, 6, 1, 2),
          "a date with no seconds parses")
    check(parse_when("2026-02-30 01:00:00") is None,
          "30 February is refused, not rolled over")
    check(parse_int(" 12 ") == 12 and parse_int("1_000") is None
          and parse_int("") is None,
          "integers parse strictly: no underscores, no blanks")
    check(parse_bit(" 1 ") is True and parse_bit("") is None,
          "a blank bit is unreadable, not false")

    # ---- overwrites: a fixed name with INIT
    fixed = "D:\\bak\\appdb.bak"
    v = _run([_row(20, path=fixed, finish="2026-10-04 01:00:00"),
              _row(21, path=fixed, finish="2026-10-05 01:00:00"),
              _row(22, path=fixed, finish="2026-10-06 01:00:00")])[0]
    check(v["status"] == "WARN" and v["chosen"]["id"] == 22,
          "a fixed file name with INIT passes as WARN, not PASS  "
          "<-- pinned defect")
    check("the 1 earlier set(s)" in v["warnings"][0],
          "and the warning counts the earlier set that is gone")
    lost, rewrote = overwrites(read_export(_csv(
        [_row(20, path=fixed), _row(21, path=fixed), _row(22, path=fixed)])))
    check(lost == {("srv-a", 20): 21, ("srv-a", 21): 22} and
          rewrote == {("srv-a", 20): 0, ("srv-a", 21): 1, ("srv-a", 22): 1},
          "each INIT run destroys the set before it")
    v = _run([_row(20, path=fixed, finish="2026-10-06 01:00:00"),
              _row(21, btype="L", path=fixed, finish="2026-10-06 02:00:00")])[0]
    check(v["code"] == "NO-FULL" and "backup set 21" in v["skipped"][0][1][0],
          "a log backup written WITH INIT over the full destroys it  "
          "<-- pinned defect")
    v = _run([_row(20, path=fixed, finish="2026-10-06 01:00:00"),
              _row(21, btype="L", path=fixed, pos="2",
                   finish="2026-10-06 02:00:00")])[0]
    check(v["status"] == "PASS",
          "a log appended at position 2 (NOINIT) destroys nothing")
    v = _run([_row(20, path=fixed, finish="2026-10-06 01:00:00"),
              _row(21, btype="L", path=fixed, pos="",
                   finish="2026-10-06 02:00:00")])[0]
    check(v["code"] == "NO-FULL",
          "a later writer with no position is a rewrite: unknown is not proof")
    v = _run([_row(20, path=fixed, finish="2026-10-06 01:00:00"),
              _row(21, db="otherdb", path=fixed,
                   finish="2026-10-06 02:00:00")], databases=["appdb"])[0]
    check(v["code"] == "NO-FULL",
          "another database's backup to the same file destroys this one  "
          "<-- pinned defect")
    v = _run([_row(20, path=fixed, finish="2026-10-06 01:00:00"),
              _row(21, path="d:\\BAK\\APPDB.BAK", btype="I",
                   finish="2026-10-06 02:00:00")])[0]
    check(v["code"] == "NO-FULL",
          "a path that differs only in case is the same file")
    v = _run([_row(20, path=fixed, finish="2026-10-06 01:00:00"),
              _row(21, path=fixed, server="srv-b",
                   finish="2026-10-06 02:00:00")], server="srv-a")[0]
    check(v["status"] == "PASS",
          "the same path on another server is another file")
    v = _run([_row(20, path="D:\\bak\\s1.bak", finish="2026-10-06 01:00:00"),
              _row(20, path="D:\\bak\\s2.bak", finish="2026-10-06 01:00:00"),
              _row(21, btype="L", path="D:\\bak\\s2.bak",
                   finish="2026-10-06 02:00:00")])[0]
    check(v["code"] == "NO-FULL",
          "a striped set with one stripe rewritten is refused")

    # ---- which full, which database
    v = _run([good, _row(23, btype="I", finish="2026-10-06 12:00:00")])[0]
    check(v["chosen"]["id"] == 10,
          "a newer differential is not a full and is not chosen")
    v = _run([_row(24, btype="d")])[0]
    check(v["status"] == "PASS", "a lower-case type d reads as D")
    v = _run([_row(25, rec="SIMPLE")])[0]
    check(v["status"] == "WARN" and "SIMPLE" in v["warnings"][0],
          "a full taken under SIMPLE recovery passes as WARN")
    v = _run([_row(25, rec="")])[0]
    check(v["status"] == "WARN" and "recovery_model ''" in v["warnings"][0],
          "a blank recovery model is a WARN, never assumed FULL")
    v = _run([_row(25, rec="bulk-logged")])[0]
    check(v["status"] == "PASS", "BULK-LOGGED reads in any case")
    vs = _run([good], databases=["appdb", "ghostdb"])
    check([x["code"] for x in vs] == ["OK", "NO-ROWS"],
          "a requested database with no rows is FAIL, never skipped  "
          "<-- pinned defect")
    vs = _run([good], databases=["AppDB"])
    check(vs[0]["code"] == "NO-ROWS",
          "database names match exactly: AppDB is not appdb")
    vs = _run([good, _row(30, db="otherdb", finish="2026-10-01 01:00:00")])
    check([x["status"] for x in vs] == ["PASS", "FAIL"],
          "with no --database every database in the export is judged")
    vs = _run([good, _row(31, server="srv-b", snap="1")])
    check([(x["server"], x["status"]) for x in vs] ==
          [("srv-a", "PASS"), ("srv-b", "FAIL")],
          "one server's full never passes the same database on another  "
          "<-- pinned defect")
    vs = _run([good], server="srv-z", databases=["appdb"])
    check(len(vs) == 1 and vs[0]["code"] == "NO-ROWS" and
          vs[0]["server"] == "srv-z",
          "--server naming a server absent from the export is FAIL")
    vs = _run([])
    check(len(vs) == 1 and vs[0]["status"] == "FAIL",
          "an export with no rows is FAIL, never an empty PASS  "
          "<-- pinned defect")
    check(gate(_run([_row(25, rec="SIMPLE")])) == EXIT_PASS,
          "a WARN exits 0 by default")
    check(gate(_run([_row(25, rec="SIMPLE")]), strict=True) == EXIT_FAIL,
          "--strict turns a WARN into exit 1")
    check(gate(_run([good])) == EXIT_PASS and gate(_run([snap])) == EXIT_FAIL,
          "PASS exits 0, FAIL exits 1")

    # ---- one backup_set_id on two servers: ids are unique per msdb only
    vs = _run([_row(10), _row(10, db="webdb", server="srv-b", snap="1",
                              dev="7", path="{guid-d}")])
    check([(x["server"], x["database"], x["status"]) for x in vs] ==
          [("srv-a", "appdb", "PASS"), ("srv-b", "webdb", "FAIL")],
          "the same backup_set_id on two servers is two sets, each judged  "
          "<-- pinned defect")
    check(vs[0]["chosen"]["files"] == ["D:\\bak\\appdb_10.bak"] and
          vs[0]["chosen"]["devices"] == [2],
          "and one server's files and device types never join the other's  "
          "<-- pinned defect")
    vs = _run([_row(10), _row(10, server="srv-b", finish="2026-10-01 01:00:00")],
              server="srv-b")
    check(len(vs) == 1 and vs[0]["code"] == "STALE",
          "--server finds the second server's set that shares an id")
    vs = _run([_row(10, path=fixed), _row(11, path=fixed),
               _row(10, server="srv-b", path=fixed)])
    check([(x["server"], x["status"]) for x in vs] ==
          [("srv-a", "WARN"), ("srv-b", "PASS")] and
          vs[0]["chosen"]["id"] == 11 and vs[1]["chosen"]["id"] == 10,
          "an overwrite on one server never marks the same id lost on another  "
          "<-- pinned defect")

    # ---- order of the input never changes the verdict
    v = _run([_row(22, path=fixed, finish="2026-10-06 01:00:00"),
              _row(21, path=fixed, finish="2026-10-05 01:00:00"),
              _row(20, path=fixed, finish="2026-10-04 01:00:00")])[0]
    check(v["chosen"]["id"] == 22 and v["warnings"][0].count("the 1 earlier") == 1,
          "an export in descending backup_set_id order reads the same as "
          "ascending")
    check([s["id"] for s in read_export(_csv([_row(22), _row(20), _row(21)]))] ==
          [20, 21, 22],
          "read_export returns the sets in ascending backup_set_id order")
    vs = _run([_row(40, db="zeta"), _row(41, db="alpha")])
    check([x["database"] for x in vs] == ["alpha", "zeta"],
          "verdicts list databases in name order, not export order")

    # ---- position
    v = _run([_row(20, path=fixed, pos="1"), _row(21, path=fixed, pos="2")])[0]
    check(v["status"] == "WARN" and v["chosen"]["id"] == 21 and
          "FILE = 2" in v["warnings"][0],
          "a full appended at position 2 passes as WARN and names FILE = 2  "
          "<-- pinned defect")
    check(to_record(v)["position"] == 2,
          "and the JSON record carries the position")
    for bad_pos, what in (("", "blank"), ("x", "the letter x"),
                          ("-", "a lone minus"), ("9" * 5000, "5000 digits")):
        v = _run([_row(26, pos=bad_pos)])[0]
        check(v["code"] == "NO-FULL" and
              "position %s is not an integer" % _short(bad_pos)
              in v["skipped"][0][1][0],
              "a full whose position is %s is refused  <-- pinned defect"
              % what)
    v = _run([_row(26, server="", db="")])[0]
    check(v["code"] == "NO-FULL" and
          v["skipped"][0][1][:2] == ["server_name is blank",
                                     "database_name is blank"],
          "a blank server_name or database_name refuses the row  "
          "<-- pinned defect")
    check(parse_int("9" * 5000) is None and parse_int("9" * 18) is not None,
          "an integer field longer than 18 digits is unreadable, never "
          "int()'d")
    exc = _caught(lambda: read_export(_csv([dict(good, backup_set_id="9" * 5000)])))
    check(isinstance(exc, ExportError) and "line 2" in str(exc) and
          len(str(exc)) < 120,
          "a 5000 digit backup_set_id is a short ExportError, not a traceback  "
          "<-- pinned defect")
    exc = _caught(lambda: read_export("x" * 200000 + "\n"))
    check(isinstance(exc, ExportError) and "line" in str(exc),
          "an oversize header field is an ExportError, not a csv.Error  "
          "<-- pinned defect")
    raises(lambda: _run([good], max_age_hours=float("inf")),
           "an infinite maximum age raises")

    # ---- reading the export
    hdr = ["  Backup_Set_Id "] + [c.upper() for c in COLUMNS[1:]] + ["extra"]
    sets = read_export(_csv([good], header=hdr))
    check(len(sets) == 1 and sets[0]["id"] == 10 and
          sets[0]["files"] == ["D:\\bak\\appdb_10.bak"],
          "headers are case- and space-insensitive and extra columns are ignored")
    raises(lambda: read_export("backup_set_id,type\n1,D\n"),
           "a missing column raises")
    msg = str(_caught(lambda: read_export("backup_set_id\n")))
    check("database_name" in msg and "--query" in msg,
          "and the error names the columns and points at --query")
    raises(lambda: read_export(""), "an empty file raises a column error")
    raises(lambda: read_export(_csv([dict(good, backup_set_id="-----")])),
           "a non-integer backup_set_id raises")
    exc = _caught(lambda: read_export(_csv([dict(good, backup_set_id="x")])))
    check(isinstance(exc, ExportError) and "line 2" in str(exc),
          "and the error is an ExportError naming the line")
    huge = dict(good, physical_device_name="x" * 200000)
    raises(lambda: read_export(_csv([huge])),
           "a field over the csv module's limit raises")
    short = ",".join(COLUMNS) + "\n10,srv-a,appdb,D\n"
    v = decide(read_export(short), AS_OF)[0]
    check(v["code"] == "NO-FULL",
          "a short row reads its missing cells as blank and is refused")
    check(all(c in EXPORT_SQL for c in COLUMNS),
          "the --query export names every column the reader needs")
    check("LEFT JOIN" in EXPORT_SQL and ", 121)" in EXPORT_SQL,
          "the export keeps sets with no family and dates in style 121")

    # ---- report
    v = _run([good, log, snap])[0]
    lines = describe(v)
    check(lines[0].startswith("PASS  srv-a / appdb: full set 10 finished "
                              "2026-10-06 01:00:00, 19.0 h"),
          "the report leads with the verdict, the set and its age")
    check("      file: D:\\bak\\appdb_10.bak" in lines and
          "      passed over 1 newer full set(s):" in lines,
          "and lists the file and the newer set it passed over")
    lines = describe(_run([snap])[0])
    check(lines[0] == "FAIL  srv-a / appdb: no restorable full backup in the "
                      "export" and lines[1] == "      refused 1 full set(s):",
          "a FAIL says no restorable full and lists what it refused")
    lines = describe(_run([_row(18, finish="2026-10-04 01:00:00")])[0])
    check("the limit is 26 h" in lines[0] and "67.0 h" in lines[0],
          "a STALE line gives the age and the limit")
    check(describe(_run([], databases=["ghostdb"])[0]) ==
          ["FAIL  ghostdb: no backup history rows for this database"],
          "a missing database with no server says so")
    check(describe(_run([])[0]) ==
          ["FAIL  the export holds no backup history rows"],
          "an empty export says so")
    lines = describe(_run([_row(25, rec="SIMPLE")])[0])
    check(lines[0].startswith("WARN  ") and lines[2].startswith("      warning:"),
          "a WARN prints each warning")
    check(describe(_run([_row(18, finish="garbage")])[0])[2].startswith(
        "        set 18  unreadable date  backup_finish_date"),
          "an unreadable date prints as unreadable")
    rec = to_record(_run([good, snap])[0])
    check(rec["backup_set_id"] == 10 and rec["refused"][0]["backup_set_id"] == 12
          and rec["age_hours"] == 19.0,
          "the JSON record carries the chosen set, its age and every refusal")
    rec = to_record(_run([snap])[0])
    check(rec["backup_set_id"] is None and rec["files"] == [] and
          rec["age_hours"] is None,
          "a FAIL record has no chosen set")

    # ---- the CLI, end to end over real files
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "history.csv")
        with io.open(path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(_csv([good, log, snap]))
        base = [path, "--as-of", "2026-10-06 20:00:00"]
        code, out, err = _cli(base)
        check(code == 0 and "PASS  srv-a / appdb: full set 10" in out,
              "a BOM export passes end to end and exits 0")
        code, out, err = _cli(base + ["--max-age-hours", "12"])
        check(code == 1 and "the limit is 12 h" in out,
              "--max-age-hours 12 makes the same full fail")
        code, out, err = _cli(base + ["--database", "appdb", "--database",
                                      "ghostdb"])
        check(code == 1 and "ghostdb: no backup history rows" in out,
              "--database is repeatable and a missing one exits 1")
        code, out, err = _cli(base + ["--server", "srv-b"])
        check(code == 1 and "no backup history rows" in out,
              "--server filters the export")
        code, out, err = _cli([path, "--as-of", "2026-10-06 20:00:00",
                               "--strict", "--allow-copy-only"])
        check(code == 0, "--strict with nothing to warn about still exits 0")
        report = os.path.join(tmp, "verdict.json")
        code, out, err = _cli(base + ["--out", report])
        check(code == 0 and not os.path.exists(report),
              "--out without --apply writes nothing at all  <-- pinned defect")
        check("was not written" in out, "and says what it skipped")
        code, out, err = _cli(base + ["--out", report, "--apply"])
        with io.open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        check(code == 0 and data["verdicts"][0]["backup_set_id"] == 10 and
              data["as_of"] == "2026-10-06 20:00:00",
              "--apply writes the JSON verdict with the as-of time")
        check("Wrote 1 verdict(s)" in out, "and says what it wrote")
        code, out, err = _cli(base + ["--out", tmp, "--apply"])
        check(code == 2 and "cannot write" in err,
              "an --out that cannot be written exits 2")
        code, out, err = _cli(base + ["--apply"])
        check(code == 64, "--apply without --out is a usage error")
        code, out, err = _cli(base + ["--ap"])
        check(code == 64 and "unrecognized" in err,
              "a unique prefix --ap of --apply is REFUSED by the parser  "
              "<-- pinned defect")
        code, out, err = _cli(base + ["--max-age", "1"])
        check(code == 64, "a prefix --max-age of --max-age-hours is refused")
        code, out, err = _cli([os.path.join(tmp, "absent.csv")])
        check(code == 2 and "cannot read" in err, "a missing export exits 2")
        bad = os.path.join(tmp, "utf16.csv")
        with io.open(bad, "w", encoding="utf-16") as fh:
            fh.write(_csv([good]))
        code, out, err = _cli([bad])
        check(code == 2 and "UTF-8" in err, "a UTF-16 export exits 2 and says "
                                            "to export UTF-8")
        cols = os.path.join(tmp, "cols.csv")
        with io.open(cols, "w", encoding="utf-8", newline="") as fh:
            fh.write("backup_set_id,type\n1,D\n")
        code, out, err = _cli([cols])
        check(code == 64 and "lacks column" in err,
              "an export missing a column exits 64")
        ids = os.path.join(tmp, "ids.csv")
        with io.open(ids, "w", encoding="utf-8", newline="") as fh:
            fh.write(_csv([dict(good, backup_set_id="-----")]))
        code, out, err = _cli([ids])
        check(code == 2 and "not an integer" in err,
              "an export with a non-integer set id exits 2")
        uni = os.path.join(tmp, "uni.csv")
        with io.open(uni, "w", encoding="utf-8", newline="") as fh:
            fh.write(_csv([_row(10, path=u"D:\\bak\\\u0141\u00f3d\u017a.bak")]))
        narrow = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        narrow_err = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = narrow, narrow_err
        try:
            code = main([uni, "--as-of", "2026-10-06 20:00:00"])
            main([os.path.join(tmp, "\u0141.csv")])
        finally:
            sys.stdout, sys.stderr = saved
        narrow.flush()
        narrow_err.flush()
        check(code == 0 and b"\\u0141\\xf3d\\u017a.bak" in narrow.buffer.getvalue()
              and b"\\u0141.csv" in narrow_err.buffer.getvalue(),
              "a name outside the console code page prints as an escape, "
              "never a traceback  <-- pinned defect")
        code, out, err = _cli([path])
        check(code in (0, 1) and out.startswith(
            "as of %s" % datetime.datetime.now().strftime("%Y-%m-%d")),
              "with no --as-of the ages are judged at this machine's clock")
    code, out, err = _cli(["--as-of", "yesterday", "x.csv"])
    check(code == 64 and "--as-of" in err, "an unreadable --as-of exits 64")
    code, out, err = _cli(["x.csv", "--max-age-hours", "0"])
    check(code == 64, "--max-age-hours 0 exits 64")
    code, out, err = _cli(["x.csv", "--max-age-hours", "inf"])
    check(code == 64 and "finite" in err,
          "--max-age-hours inf exits 64, so no Infinity reaches the JSON  "
          "<-- pinned defect")
    code, out, err = _cli([])
    check(code == 64 and "export CSV" in err, "no export at all exits 64")
    code, out, err = _cli(["--query"])
    check(code == 0 and out == EXPORT_SQL + "\n", "--query prints the export SQL")
    a = _parse(["x.csv"])
    check(a.apply is False and a.out is None and a.strict is False and
          a.allow_copy_only is False and a.max_age_hours == DEFAULT_MAX_AGE_HOURS,
          "every option defaults to off, and the age to the configured value")
    saved = sys.argv
    sys.argv = ["bkpbase.py", "--query"]
    try:
        code, out, err = _cli(None)
    finally:
        sys.argv = saved
    check(code == 0 and "msdb.dbo.backupset" in out,
          "main with no argv reads the arguments after the program name")
    spec = importlib.util.spec_from_file_location("_bkpbase_probe",
                                                  os.path.abspath(__file__))
    probe = importlib.util.module_from_spec(spec)
    buf, saved = io.StringIO(), sys.stdout
    sys.stdout, no_pyc = buf, sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # the probe import leaves no .pyc behind
    try:
        spec.loader.exec_module(probe)
    finally:
        sys.stdout, sys.dont_write_bytecode = saved, no_pyc
    check(buf.getvalue() == "" and probe.COLUMNS == COLUMNS,
          "importing the module runs nothing and prints nothing")

    # ---- the harness itself
    c2, r2, p2, f2 = _harness()
    buf, saved = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        c2(False, "x")
        r2(lambda: None, "y")
        r2(lambda: 1 / 0, "z")
        r2(lambda: int("q"), "w")
        bad_code = _summary(p2[0], f2)
    finally:
        sys.stdout = saved
    check(len(f2) == 3 and p2[0] == 1,
          "check() and raises() really do record a failure  <-- pinned defect")
    check(bad_code == 1 and "4 assertions, 3 failed" in buf.getvalue() and
          "  FAILED: x" in buf.getvalue(),
          "and a failed run exits 1 and names each failure")
    check(_caught(lambda: None) is None,
          "_caught returns None when nothing is raised")
    return _summary(passed[0], failed)


def _caught(fn):
    """The ValueError fn raises, or None."""
    try:
        fn()
    except ValueError as exc:
        return exc
    return None


def _summary(passed, failed):
    print("-" * 68)
    total = passed + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ======================================================================= cli

class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag. Here 2 means a file error, so use 64."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "%s: error: %s\n" % (self.prog, message))


def _parse(argv):
    ap = _Parser(
        prog="bkpbase.py",
        allow_abbrev=False,
        description="Give a restorable-full verdict over exported "
                    "msdb.backupset rows. Refuses snapshot, copy-only, "
                    "damaged and overwritten fulls.",
        epilog="Nothing is written without --apply. Exit 0 pass, 1 fail, "
               "2 read or write error, 64 usage.",
    )
    ap.add_argument("export", nargs="?",
                    help="CSV of backup history, as --query exports it")
    ap.add_argument("--database", action="append", default=None,
                    help="judge this database; repeatable. A name with no "
                         "rows fails. Default: every database in the export")
    ap.add_argument("--server", default=None,
                    help="judge only rows whose server_name is exactly this")
    ap.add_argument("--max-age-hours", dest="max_age_hours", type=float,
                    default=DEFAULT_MAX_AGE_HOURS,
                    help="oldest a restorable full may be (default %g)"
                         % DEFAULT_MAX_AGE_HOURS)
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="judge ages at this time, yyyy-mm-dd hh:mi:ss, in the "
                         "server's local time. Default: now on this machine")
    ap.add_argument("--allow-copy-only", dest="allow_copy_only",
                    action="store_true",
                    help="accept a copy-only full as the base, with a warning")
    ap.add_argument("--strict", action="store_true",
                    help="fail on a warning as well")
    ap.add_argument("--query", action="store_true",
                    help="print the T-SQL that produces the export, and exit")
    ap.add_argument("--out", default=None,
                    help="path for a JSON verdict. Needs --apply")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _usage(msg):
    print("error: %s" % msg, file=sys.stderr)
    return EXIT_USAGE


def main(argv=None):
    # A name outside the console code page must print, not crash with exit 1.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()
    if args.query:
        print(EXPORT_SQL)
        return EXIT_PASS
    if not args.export:
        return _usage("an export CSV is required. Run --query for the T-SQL "
                      "that produces it, or --self-test.")
    if not 0 < args.max_age_hours < float("inf"):
        return _usage("--max-age-hours must be above zero and finite.")
    if args.apply and not args.out:
        return _usage("--apply needs --out.")
    if args.as_of is None:
        as_of = datetime.datetime.now()
    else:
        as_of = parse_when(args.as_of)
        if as_of is None:
            return _usage("--as-of %r is not yyyy-mm-dd hh:mi:ss." % args.as_of)

    try:
        with io.open(args.export, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        print("error: %s is not UTF-8. Export it as UTF-8 and run again."
              % args.export, file=sys.stderr)
        return EXIT_READ
    except (IOError, OSError) as exc:
        print("error: cannot read %s: %s" % (args.export, exc), file=sys.stderr)
        return EXIT_READ
    try:
        sets = read_export(text)
    except ColumnError as exc:
        return _usage(str(exc))
    except ExportError as exc:
        print("error: %s: %s" % (args.export, exc), file=sys.stderr)
        return EXIT_READ

    verdicts = decide(sets, as_of, args.max_age_hours, args.database,
                      args.server, args.allow_copy_only)
    print("as of %s, maximum age %g h, %d backup set(s) read"
          % (_fmt(as_of), args.max_age_hours, len(sets)))
    for v in verdicts:
        for line in describe(v, args.max_age_hours):
            print(line)
    code = gate(verdicts, args.strict)

    if args.out and not args.apply:
        print("\nCheck only. %s was not written. Re-run with --apply."
              % args.out)
    elif args.out:
        record = {"as_of": _fmt(as_of), "max_age_hours": args.max_age_hours,
                  "strict": args.strict, "exit_code": code,
                  "verdicts": [to_record(v) for v in verdicts]}
        try:
            with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        except (IOError, OSError) as exc:
            print("error: cannot write %s: %s" % (args.out, exc),
                  file=sys.stderr)
            return EXIT_READ
        print("\nWrote %d verdict(s) to %s." % (len(verdicts), args.out))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
