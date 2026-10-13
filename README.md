# bkpbase

Give a restorable-full verdict over exported `msdb.backupset` rows. Refuse snapshot rows, copy-only
rows, and fulls that a later backup overwrote.

A schema upgrade had a hard gate in front of it: no recent full backup, no upgrade. The gate asked
`msdb` for the newest full backup of the database, and `msdb` gave it one. The row had type `D`, it
was not copy-only, and it was recent. The gate passed.

That row was a storage snapshot. A third-party backup agent took one every hour and recorded each
one in `msdb` as a regular full backup. There was no `.bak` file behind it, so there was nothing
that a plain `RESTORE DATABASE` could read.

The gate did reject copy-only backups, but it checked only the newest row. These snapshots were
regular fulls, so the check did not fire. On a second server the snapshots were copy-only. There
the newest row was a snapshot, the gate rejected it, and it never looked at the good native full
one row further back. One gate failed open on one server and failed closed on the other.

This tool reads the rows, walks back from the newest full, and stops at the newest one whose row
describes a native backup file that a plain `RESTORE DATABASE` could read. It reads history only and
never opens the file: see Limits. Then it checks the age of that full.

```
$ python bkpbase.py --self-test
bkpbase self-test: no server, no network, synthetic history only
--------------------------------------------------------------------
PASS  a newer non-copy-only snapshot full is passed over, never chosen  <-- pinned defect
PASS  and the snapshot is listed as the newer row it refused
PASS  and the reason names it a snapshot
PASS  and names the virtual device it was written to
PASS  the chosen full is 19.0 h old at the as-of time
PASS  a history of snapshot fulls only is FAIL, not PASS  <-- pinned defect
PASS  a newer copy-only snapshot does not block an older good full  <-- pinned defect
PASS  a snapshot whose metadata file went TO DISK is still refused  <-- pinned defect
PASS  a copy-only full is refused as the base by default
PASS  and the reason says it cannot be a differential base
...
PASS  a set with no media family row (LEFT JOIN blank) is refused  <-- pinned defect
...
PASS  a locale date is refused, never guessed  <-- pinned defect
PASS  an unreadable is_snapshot refuses the row, never reads as 0  <-- pinned defect
...
PASS  a fresh snapshot cannot hide a stale real full  <-- pinned defect
...
PASS  a fixed file name with INIT passes as WARN, not PASS  <-- pinned defect
...
PASS  a log backup written WITH INIT over the full destroys it  <-- pinned defect
...
PASS  another database's backup to the same file destroys this one  <-- pinned defect
...
PASS  a requested database with no rows is FAIL, never skipped  <-- pinned defect
...
PASS  one server's full never passes the same database on another  <-- pinned defect
...
PASS  an export with no rows is FAIL, never an empty PASS  <-- pinned defect
...
PASS  the same backup_set_id on two servers is two sets, each judged  <-- pinned defect
PASS  and one server's files and device types never join the other's  <-- pinned defect
...
PASS  an overwrite on one server never marks the same id lost on another  <-- pinned defect
...
PASS  a full appended at position 2 passes as WARN and names FILE = 2  <-- pinned defect
...
PASS  a full whose position is blank is refused  <-- pinned defect
PASS  a full whose position is the letter x is refused  <-- pinned defect
PASS  a full whose position is a lone minus is refused  <-- pinned defect
PASS  a full whose position is 5000 digits is refused  <-- pinned defect
PASS  a blank server_name or database_name refuses the row  <-- pinned defect
...
PASS  a 5000 digit backup_set_id is a short ExportError, not a traceback  <-- pinned defect
PASS  an oversize header field is an ExportError, not a csv.Error  <-- pinned defect
...
PASS  --out without --apply writes nothing at all  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  a name outside the console code page prints as an escape, never a traceback  <-- pinned defect
...
PASS  --max-age-hours inf exits 64, so no Infinity reaches the JSON  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and a failed run exits 1 and names each failure
PASS  _caught returns None when nothing is raised
--------------------------------------------------------------------
128 assertions, 0 failed
```

The full run prints all 128 assertions. Each `...` line marks a cut in this block.

## Requirements

Python 3.9 or newer and nothing else. No `pyodbc`, no SQL Server client, no third-party package, no
network. The tool reads a CSV that you export from `msdb` with any client you already have.

The self-test prints 128 assertions on Windows (Python 3.13.2, 3.12.10 and 3.9.25) and on Ubuntu
(Python 3.12.3). The count is the same on all four. Branch coverage of `--self-test` is 100 percent,
measured with `coverage` 7.16.1 on Windows Python 3.13.2. It was not measured on Ubuntu, where
`coverage` is not installed.

```
git clone https://github.com/uhsear/bkpbase.git
```

## Usage

1. Print the export query, and run it on the server whose backups you want to gate:

   ```
   python bkpbase.py --query > export.sql
   ```

2. Save the result as a UTF-8 CSV with a header row. In Windows PowerShell 5.1,
   `Export-Csv -NoTypeInformation -Encoding UTF8` writes a byte order mark, quotes every value, and
   writes bits as `True` and `False`. The tool reads that file, and a run over one confirmed it.
   The rows in that run were built in PowerShell, not read with `Invoke-Sqlcmd`.

3. Run the gate:

   ```
   python bkpbase.py history.csv --database appdb
   python bkpbase.py history.csv --database appdb --database webdb --strict
   python bkpbase.py history.csv --server srv-a --max-age-hours 26
   python bkpbase.py history.csv --out verdict.json --apply
   ```

This is a real run over a synthetic export with three databases, as of 09:00. Save this as
`history.csv` to repeat it:

```
backup_set_id,server_name,database_name,type,is_copy_only,is_snapshot,is_damaged,recovery_model,backup_finish_date,position,device_type,physical_device_name
101,srv-a,appdb,D,0,0,0,FULL,2026-10-05 01:00:12.000,1,2,D:\bak\appdb_101.bak
102,srv-a,appdb,L,0,0,0,FULL,2026-10-05 13:00:00.000,1,2,D:\bak\appdb_log.trn
103,srv-a,appdb,D,1,1,0,FULL,2026-10-05 22:10:40.000,1,7,{5f1c2a9e-0000-4000-8000-00000000000b}
104,srv-a,appdb,D,0,1,0,FULL,2026-10-06 06:00:05.000,1,7,{5f1c2a9e-0000-4000-8000-00000000000c}
105,srv-a,assetdb,D,0,0,0,FULL,2026-10-05 00:30:00.000,1,2,E:\bak\assetdb.bak
106,srv-a,assetdb,D,0,0,0,FULL,2026-10-06 00:31:00.000,1,2,E:\bak\assetdb.bak
107,srv-a,webdb,D,0,0,0,FULL,2026-10-06 02:00:00.000,1,2,D:\bak\webdb_20261006.bak
108,srv-a,webdb,D,1,1,0,FULL,2026-10-06 03:10:00.000,1,7,{5f1c2a9e-0000-4000-8000-00000000000d}
```

The run:

```
$ python bkpbase.py history.csv --as-of "2026-10-06 09:00:00"
as of 2026-10-06 09:00:00, maximum age 26 h, 8 backup set(s) read
FAIL  srv-a / appdb: the newest restorable full, set 101, finished 2026-10-05 01:00:12, 32.0 h before the as-of time; the limit is 26 h
      passed over 2 newer full set(s):
        set 104  2026-10-06 06:00:05  snapshot: the data is in a storage snapshot, not in the backup file; device type 7 (virtual device): not a disk or URL backup file
        set 103  2026-10-05 22:10:40  snapshot: the data is in a storage snapshot, not in the backup file; copy-only: it cannot serve as a differential base; device type 7 (virtual device): not a disk or URL backup file
WARN  srv-a / assetdb: full set 106 finished 2026-10-06 00:31:00, 8.5 h before the as-of time
      file: E:\bak\assetdb.bak
      warning: this backup started its file again at position 1, so the 1 earlier set(s) history shows in that file are gone; a fixed file name keeps one full, and the next INIT run to that name overwrites this one
PASS  srv-a / webdb: full set 107 finished 2026-10-06 02:00:00, 7.0 h before the as-of time
      file: D:\bak\webdb_20261006.bak
      passed over 1 newer full set(s):
        set 108  2026-10-06 03:10:00  snapshot: the data is in a storage snapshot, not in the backup file; copy-only: it cannot serve as a differential base; device type 7 (virtual device): not a disk or URL backup file
```

The newest-row gate passes `appdb` on set 104. This run fails it, because the newest full with a
file behind it is 32 hours old. The run exits 1. The same command printed the same lines on Ubuntu.

| Flag | Default | What it does |
|---|---|---|
| `EXPORT` | none | The CSV of backup history. Required unless `--query` or `--self-test`. |
| `--database` | every database in the export | Judge this database. Repeatable. A name with no rows fails. |
| `--server` | every server in the export | Judge only rows whose `server_name` is exactly this. |
| `--max-age-hours` | `26` | The oldest that the restorable full may be. Must be above zero and finite. |
| `--as-of` | now, on this machine | Judge ages at this time, `yyyy-mm-dd hh:mi:ss`, in the server's local time. |
| `--allow-copy-only` | off | Accept a copy-only full as the base, with a warning. |
| `--strict` | off | Fail on a warning as well. |
| `--query` | off | Print the T-SQL that produces the export, and exit. |
| `--out` | off | Write a JSON verdict to this path. Needs `--apply`. |
| `--apply` | off | Write `--out`. Without it, nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

Every parser flag is exact. `--ap` is not read as `--apply`, and the self-test pins that refusal.

A file name or database name that the console code page cannot print appears as a backslash escape,
such as `\u0141`. It does not stop the run.

## The export

`--query` prints this. It was written against the Microsoft documentation for each column. It has
not been run against a live SQL Server in this repository: see Limits.

```sql
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
```

- The bits are cast to `tinyint`, so every client writes `0` or `1`. The reader also accepts
  `True` and `False`, and the self-test pins that.
- The date uses style 121, `yyyy-mm-dd hh:mi:ss.mmm`, so no client applies its own locale. A date
  in any other shape refuses its row. The tool never guesses whether `10/6` is October or June.
- The join is a `LEFT JOIN`. A backup set with no media family row still arrives, with a blank
  device type, and the tool refuses it.
- A striped or mirrored backup arrives as one row per media family. The tool groups the rows on
  `server_name` and `backup_set_id` together and keeps every file name. The id is unique only inside
  one `msdb` [4], so two servers in one export can share an id, and the self-test pins that case.
- Header names are matched without regard to case or surrounding spaces. Extra columns are ignored.

## What it checks

The tool takes the full backups (type `D`) of each database, newest `backup_set_id` first. It
refuses a full for any of these reasons, and moves on to the next older one:

| Refusal | The fact it relies on |
|---|---|
| `is_snapshot = 1` | A snapshot backup writes only metadata to its file. "The output doesn't contain any of the database contents except for the header and the file contents." A restore needs the storage snapshot as well. [1] |
| `is_copy_only = 1` | A copy-only full "can't serve as a differential base." [2] `--allow-copy-only` accepts it with a warning. |
| `device_type` not 2 (disk) or 9 (Azure Storage, a URL) | 5 is tape, 7 is a virtual device, 105 is a permanent backup device. [3] Any other value, such as 102, is not in that list and is refused. A blank device type is refused too. |
| No `physical_device_name` | No file is recorded, so there is nothing to restore from. |
| `is_damaged = 1` | Damage was detected, and the backup "was requested to continue despite errors." [4] |
| A later set started the same file at position 1 | `INIT` overwrites the backup sets on the device, when their expiry allows it. `NOINIT`, the default, appends. [5] A set at position 1 is the first in its file, so every earlier set there is gone. |
| `backup_finish_date` after `--as-of` | A clock or an export problem. The tool does not trust the row. |
| A bit, a date or a position it cannot read, or a blank `server_name` or `database_name` | Unknown is refused. It never reads as `0`. `position` "Can be NULL" [4], so a blank position refuses the full. |

The newest full that survives is the base. If none survives, the database fails. If the base is
older than `--max-age-hours`, the database fails, and the line names the age and the limit. The
limit is inclusive.

The overwrite check runs over the whole export before any `--database` filter. A backup of a
different database to the same file name also overwrites this one. Paths compare without regard to
case, and only within one `server_name`. The tool keys every set on the server as well as the id.

A base that passes can still carry a warning. A warning prints `WARN` and exits 0, unless you pass
`--strict`:

- The full was taken under the SIMPLE recovery model. Microsoft's guidance says to take "a full
  or differential database backup to start the log chain" after a switch from SIMPLE. [6] The
  `begins_log_chain` column [4] is the direct record of a log chain start. The tool does not read it.
- The full sits at position 2 or later in its file. A plain `RESTORE` uses `FILE = 1` by default [10],
  so it would restore the first set in that file. The warning gives the `FILE = n` to use.
- The `recovery_model` value is not FULL, BULK-LOGGED or SIMPLE.
- The full is copy-only, and `--allow-copy-only` accepted it.
- The full itself started its file at position 1 over earlier sets. That is a fixed file name.
  Each run keeps one full, and the next `INIT` run to that name overwrites it.

## Why the obvious version is wrong

The obvious gate reads one row:

```sql
SELECT TOP (1) backup_finish_date
FROM msdb.dbo.backupset
WHERE database_name = @db AND type = 'D'
ORDER BY backup_finish_date DESC;
```

Microsoft's own example query labels every type `D` row as "Full Database" or "Full Copy-Only
Database." [4] That example does not read `is_snapshot`. A snapshot is a type `D` row, so this gate
passes on it.

Each single-column fix leaves a hole:

- `AND is_copy_only = 0` still passes a snapshot that is a regular full.
- `AND device_type IN (2, 9)` still passes a T-SQL snapshot backup. That feature writes its
  metadata file `TO DISK` or `TO URL`. [1] Its row has `is_snapshot = 1`. Its file holds no data.
- `TOP (1)` with every filter still answers the wrong question when the history holds sets that a
  later `INIT` overwrote. The row is in `msdb`. The backup is not in the file.

The tool applies every check to each row, and it reports each newer row that it passed over, with
its reasons. A reviewer can see why the gate did not pick the newest row.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every database passed. A `WARN` counts as a pass without `--strict`. |
| 1 | At least one database failed: no restorable full, a full that is too old, or no rows at all. |
| 2 | The export could not be read, or `--out` could not be written. |
| 64 | Usage error, including an unknown or abbreviated flag and a missing column. |

argparse exits 2 on a bad flag by default. This tool changes that to 64, so that 2 always means a
file problem.

## What already exists

[dbatools](https://dbatools.io) is the standard PowerShell toolkit for this work, and it does more
than this tool.

- `Get-DbaDbBackupHistory` returns the history rows. It excludes copy-only backups by default and
  filters by device type. Its documentation does not mention snapshots. [7]
- `Test-DbaLastBackup` restores the latest full backups under a test name, runs `DBCC CHECKDB`, and
  drops the copy. [8] A restore that works is stronger proof than any reading of history. Use it
  when you have the disk space and the time.

This tool is for the gate in front of a risky change, when a test restore is not possible. It
judges history that you exported, so it needs no connection, no module, and no permission on the
server. It returns a verdict, not rows.

On 2026-10-13, `gh search repos bkpbase` returned only this repository, `uhsear/bkpbase`.
The searches "backupset is_snapshot", "msdb backupset restorable", "msdb backupset check last full
backup" and "sql server backup history snapshot copy-only" returned nothing the same day.

## Limits

- It reads history, not files. A `.bak` that was deleted, moved or damaged after the backup still
  looks restorable here. A row that records a regular full for a path that was never written passes
  too, and so does a full written to the `NUL` device. Run `RESTORE VERIFYONLY`, or
  `Test-DbaLastBackup`, to check the file.
- The export query was written from the documentation and was not run against a live SQL Server
  for this repository. The column meanings come from [3] and [4]. The style 121 format comes from
  [9]. Check the first export by eye.
- The T-SQL snapshot row shape in "Why the obvious version is wrong" follows from the documented
  syntax. A live `BACKUP ... WITH METADATA_ONLY` row was not observed.
- Ages compare `backup_finish_date` against `--as-of`. The tool assumes that both use one time
  zone, the server's. The `time_zone` column [4] gives the difference from UTC "where the backup
  operation is taking place", and the tool does not read it. The default `--as-of` is this machine's
  clock. If the two are in different time zones, pass `--as-of` in the server's time.
- It does not walk the log chain. It does not check that log or differential backups after the
  base are complete, or that the LSNs line up. It judges the base only.
- The default export window is 14 days. A database whose last native full is older shows as "no
  restorable full", not as "too old". Both fail. Without `--database`, the tool judges only
  databases that appear in the export, so a database with no rows in the window is not reported at
  all. Pass `--database` for each database that you gate.
- `--database ''` matches no row, so the report says "the export holds no backup history rows". The
  wording is misleading. The run still fails.
- Database and server names match exactly, including case. A name typed in a different case fails
  as "no backup history rows". It never passes.
- Paths compare without regard to case, which suits Windows. On Linux, two paths that differ only
  in case count as one file. That error can refuse a full. It cannot pass one.
- An overwrite is found only when two backups spell the same path the same way, apart from case.
  `D:\bak\a.bak`, `D:/bak/a.bak`, `D:\bak\\a.bak`, `D:\bak\.\a.bak`, an 8.3 short name and a
  UNC share of the same folder count as different files. An overwrite through another spelling is
  not found, and the fixed-file-name warning is missed.
- The overwrite check sees one `msdb` at a time. A backup from another server to the same shared
  path or URL is not counted as an overwrite.
- The export has no `mirror` column. A mirrored set arrives as several file names for one set. If
  one copy was overwritten, the tool refuses the whole set, although another copy may still restore.
  This fails closed.
- A backup to a device with a `device_type` that [3] does not list, such as 102, is refused, even
  when the device is a disk file. The result is a false FAIL, never a false PASS.
- [3] says `physical_device_name` may hold the original restore source path instead of the backup
  destination. The tool treats every value as the destination.
- Device names print, and go into `--out`, exactly as exported. Check an export for a query string
  such as a SAS signature before you share a report.
- The reader is strict about shape. A `sqlcmd` "(N rows affected)" trailer line, or a semicolon or
  tab delimited file, gives exit 2 or 64, not a verdict. Strip the trailer and use commas.
- A final row that was cut inside the file name still reads as a row with a shorter path. Only a cut
  before the last cell is refused as a short row.
- `msdb` history can be pruned with `sp_delete_backuphistory`. [4] Pruned rows are invisible here,
  so a set that overwrote a pruned set does not get the fixed-file-name warning.
- It opens no socket and imports no network module. No network path exists to test.
- Nothing is written without `--apply`, and only `--out` is ever written, with one exception:
  `--self-test` writes files only inside its own temporary directory, which it deletes.
- `--apply` replaces the `--out` file without asking, even when it is the export that the run just
  read. The write is not atomic.

## References

1. Microsoft Learn, "Create a Transact-SQL snapshot backup".
   https://learn.microsoft.com/en-us/sql/relational-databases/backup-restore/create-a-transact-sql-snapshot-backup
2. Microsoft Learn, "Copy-Only Backups".
   https://learn.microsoft.com/en-us/sql/relational-databases/backup-restore/copy-only-backups-sql-server
3. Microsoft Learn, "backupmediafamily (Transact-SQL)".
   https://learn.microsoft.com/en-us/sql/relational-databases/system-tables/backupmediafamily-transact-sql
4. Microsoft Learn, "backupset (Transact-SQL)".
   https://learn.microsoft.com/en-us/sql/relational-databases/system-tables/backupset-transact-sql
5. Microsoft Learn, "BACKUP (Transact-SQL)", the `{ NOINIT | INIT }` option.
   https://learn.microsoft.com/en-us/sql/t-sql/statements/backup-transact-sql
6. Microsoft Learn, "Set database recovery model".
   https://learn.microsoft.com/en-us/sql/relational-databases/backup-restore/view-or-change-the-recovery-model-of-a-database-sql-server
7. dbatools, "Get-DbaDbBackupHistory". https://dbatools.io/Get-DbaDbBackupHistory
8. dbatools, "Test-DbaLastBackup". https://dbatools.io/Test-DbaLastBackup
9. Microsoft Learn, "CAST and CONVERT (Transact-SQL)", date and time styles.
   https://learn.microsoft.com/en-us/sql/t-sql/functions/cast-and-convert-transact-sql
10. Microsoft Learn, "RESTORE Arguments (Transact-SQL)", the backup-set `FILE` option. "The
    default backup set file number is 1."
    https://learn.microsoft.com/en-us/sql/t-sql/statements/restore-statements-arguments-transact-sql

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [compressfloor](https://github.com/uhsear/compressfloor) - the same shape for an enterprise
  geodatabase: a verdict from exported system tables, with no connection to the server.
- [safe-republish](https://github.com/uhsear/safe-republish) - refuse a destructive step until a
  check passes. Put this gate in front of it.
- [taskpulse](https://github.com/uhsear/taskpulse) - find the scheduled task that fails silently,
  which is often the backup job itself.
