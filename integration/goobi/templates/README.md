# Process templates

Goobi keeps workflow templates only in MariaDB, so the versioned copy of a
template is an SQL dump made by `../export_template.sh`. One file per
template, named for the template's exact title:

```sh
# on the Goobi host, from the repo checkout
integration/goobi/export_template.sh Capture_To_Ingest_v1 \
    > integration/goobi/templates/Capture_To_Ingest_v1.sql
```

`drift_check.sh` re-exports every template that has a file here and diffs
it, so a step added or reordered in the Goobi web interface shows up as drift
until the new export is committed.

**`Capture_To_Ingest_v1.sql`** was harvested from the old WSL host on
2026-10-15 (`ProzesseID 12`, steps 52-60 and 70, every step assigned to user
group 1; a fresh install seeds processes 1 and 4 and steps up to 28, so the
IDs are free on a new server). Its ten steps are the stage table in
PROVISIONING.md section 5 in order: Capture, QC1, Post-Process, Import Images
(script), QC2 (`intranda_step_imageQA`), OCR (script), Transcript Correction
(metadata + images), Review to Upload, Review to Remove
(`intranda_delay_30_days`, delay step), Delete Images. On the new server,
run the pre-flight queries in its header, then
`mysql goobi < Capture_To_Ingest_v1.sql` and restart Tomcat. Group 1 is the
stock *Administration* group: reassign the steps to real groups in the web
interface once users exist, then re-export.

The dump was written by MariaDB 10.11.14's `mysqldump`, which opens each
table's section with `/*M!999999\- enable the sandbox mode */`. MariaDB
10.11.8 and later accept it; an older `mysql` client, or MySQL, stops with
*Unknown command '\-'*. Delete those lines if the target's client is older.

The file committed here is the source of truth. Re-export
and commit after every template change made in the web interface.

Importing never happens automatically: `install.sh` does not touch the
database, because a template import with a missing dependency succeeds
silently and fails later at process creation (PROVISIONING.md section 7).
