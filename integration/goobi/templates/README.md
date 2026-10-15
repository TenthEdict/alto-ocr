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

**This folder is empty on purpose.** The only built template so far,
`Capture_To_Ingest_v1` (10 steps; see PROVISIONING.md section 5), lived on a
WSL host that is no longer reachable, and an SQL dump cannot be reconstructed
by hand without risking the ID collisions `export_template.sh` guards
against. On the new server, either:

- **import** it, if the old host comes back: export there, commit the file,
  run the pre-flight queries in its header on the new host, then
  `mysql goobi < Capture_To_Ingest_v1.sql` and restart Tomcat; or
- **rebuild** it in the web interface from the stage table in PROVISIONING.md
  section 5, then export it here straight away.

Either way the file committed here becomes the source of truth. Re-export
and commit after every template change made in the web interface.

Importing never happens automatically: `install.sh` does not touch the
database, because a template import with a missing dependency succeeds
silently and fails later at process creation (PROVISIONING.md section 7).
