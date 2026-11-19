# xnat_fix_ohif_uids

Fix XNAT imaging sessions that show **"No viewable scans"** in the OHIF
viewer, by backfilling missing `SeriesInstanceUID`s on scan rows from the
DICOM file headers and regenerating the viewer session metadata.

[![License: BSD 3-Clause](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](LICENSE)

## The problem

Clicking the **OHIF Viewer** button on an XNAT session shows:

```
No viewable scans: There are no scans in this session compatible
with the OHIF Viewer.
```

The OHIF plugin builds its session metadata by walking the session's DICOM
files and matching each file's `SeriesInstanceUID` against the UID stored
on the scan row (`xnat:imageScanData/UID`). Sessions ingested by REST
scripts (rather than XNAT's standard upload pipelines) often end up with
scan rows whose `UID` field was never populated — so every instance is
dropped and an empty study list gets cached.

This script:

1. Walks every scan of the experiment. Scans without DICOM files (QC
   snapshots, result archives, derived data) are skipped — OHIF only needs
   UIDs on real image series.
2. Reads the `SeriesInstanceUID` from each scan's DICOM files — directly
   from the archive directory when it is mounted and readable (fast), or by
   downloading just the first MiB of each file via REST otherwise.
3. POSTs the UID onto the scan row via the XNAT REST field-edit syntax
   (**only with `--write`; by default it stops after showing what it would
   change**).
4. Forces the OHIF plugin to rebuild its cached session metadata, then
   reports the resulting study/series/instance counts.

## Requirements

- Python 3.7+
- XNAT 1.7/1.8/1.9 with the OHIF viewer plugin
- An account with edit permissions on the target experiments (the metadata
  regeneration step may additionally require admin rights on some XNAT
  versions)

```bash
pip install requests pydicom
```

## Usage

```
python xnat_fix_ohif_uids.py https://xnat.example.org user EXPERIMENT_ID
```

(prompts for the password). Then check the report, and re-run with
`--write` to apply:

```
python xnat_fix_ohif_uids.py https://xnat.example.org user EXPERIMENT_ID --write
```

Fix every experiment in a project (safe by default — see
[Conservative defaults](#conservative-defaults)):

```
python xnat_fix_ohif_uids.py https://xnat.example.org user --all --project MYPROJECT --write
```

Full options:

```
usage: xnat_fix_ohif_uids.py [-h] [--project PROJECT] [--verify N] [--dry-run]
                             [--write] [--overwrite] [--insecure]
                             [--workers N] [--full-download]
                             [--archive-root PATH] [--no-local-files] [--all]
                             [--include-non-imaging] [--no-colour]
                             [--basic-auth]
                             host username [password] [experiment_id]

positional arguments:
  host                  XNAT host, e.g. https://xnat.example.org
  username              XNAT username
  password              XNAT password (omit to be prompted)
  experiment_id         experiment ID (accession number) or label (omit when
                        using --all)

optional arguments:
  -h, --help            show this help message and exit
  --project PROJECT     project ID (default: experiment's primary project)
  --verify N            read N files per scan to confirm a consistent
                        SeriesInstanceUID (default 1)
  --dry-run             show planned UID updates, change nothing (this is the
                        default; kept for explicitness)
  --write, --apply      apply the UID updates and regenerate the viewer
                        metadata (without this flag the script only reports
                        what it would do)
  --overwrite           process experiments even when their uididmap already
                        has entries (default: skip any session whose
                        viewer/ROI references already resolve; only fully-
                        broken sessions are processed)
  --insecure            skip TLS certificate verification (self-signed certs)
  --workers N           parallel scans (default 1: sequential; raise to speed
                        up large sessions, at some load cost to XNAT)
  --full-download       if a file's header cannot be parsed, download the
                        whole file as a fallback (slow; only for non-standard
                        DICOM files)
  --archive-root PATH   XNAT archive root; when
                        <root>/<project>/arc001/<label>/SCANS is readable,
                        DICOM headers are parsed straight from disk (default:
                        /data/xnat/archive)
  --no-local-files      never touch the local filesystem; always download file
                        headers via REST
  --all                 process every experiment in the project given by
                        --project (requires --project)
  --include-non-imaging
                        with --all, also process experiments whose type is not
                        an imaging session (skipped by default)
  --no-colour, --no-color
                        disable coloured terminal output (also disabled
                        automatically when stdout is not a terminal; NO_COLOR
                        env honoured)
  --basic-auth          authenticate every request with the username/password
                        (the old behaviour) instead of one session-token login
```

## Conservative defaults

The script is deliberately cautious on three fronts:

- **Dry run by default.** Without `--write` nothing is modified; the script
  reports exactly which scan rows would change and to which UID.
- **Existing references are protected.** Before processing an experiment,
  the plugin's uididmap (`series UID → scan ID`) is checked. If it already
  has entries — i.e. viewer/ROI references already resolve — the session is
  skipped. Only sessions that are *completely* broken (no UIDs anywhere)
  are fixed. `--overwrite` lifts this guard.
- **Gentle on the server.** One login per run (`POST /data/JSESSIONID`)
  with the session cookie reused for all further requests, so the auth
  backend (LDAP etc.) never sees repeated logins that could trip
  failed-login lockouts or rate limits; sequential processing by default
  (`--workers` to parallelise).

If the session cookie is not honoured on REST calls (SSO/proxy front-ends,
or a `Secure`-cookie configuration accessed over plain HTTP), the script
detects the cause and either recovers (stripping the Secure flag for
loopback runs) or falls back to per-request credentials automatically.

## ROI safety

Saved ROI collections (`icr:roiCollectionData` assessors) are not touched:
the script writes only the `UID` field of scan rows and the viewer's cached
session JSON. Because the UID written is read from the DICOM files
themselves — the same UIDs ROIs reference — fixing a session that could
not display is what enables ROIs to be saved and displayed on it. Sessions
with existing, working ROI references are skipped by default (see above).

Scans whose files carry **multiple different** SeriesInstanceUIDs are
flagged (`WARN`) rather than silently fixed; review those by hand.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success (including intentional skips) |
| 1 | at least one scan failed, or an experiment errored |
| 2 | the run could not start (bad arguments, inaccessible experiment/project) |

## Author

**Baris Kanber, PhD**

## License

Released under the [BSD 3-Clause License](LICENSE) — free to use,
distribute, and modify, provided credit is retained.
