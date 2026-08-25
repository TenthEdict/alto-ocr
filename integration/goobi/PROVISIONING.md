# Provisioning a Goobi host

What the reference install script gets wrong or leaves undone, recorded from a
working build so the next host is a checklist rather than an archaeology
session. Everything here was verified on the live instance.

**Reference host:** Ubuntu 24.04.4 LTS · Goobi workflow 26.06.1
(`io.goobi.workflow:workflow-core`) · Tomcat 10 on OpenJDK 21.0.11 ·
Apache 2.4 reverse proxy · MariaDB · OpenLDAP + nslcd + Samba.

Goobi's published installer targets an older stack (Tomcat 9, Java 17), so
several of its steps fail or silently no-op on 24.04. Items below are ordered
by when they bite.

---

## 1. Tomcat will not start out of the box

**Java 17+ rejects the CMS garbage-collector flags** the installer writes.
`/etc/default/tomcat10` must not contain `CMSClassUnloadingEnabled` —
`Unrecognized VM option` is fatal, not a warning. Working `JAVA_OPTS`:

```sh
JAVA_OPTS="-Djava.awt.headless=true -Xmx2g -Xms2g"
JAVA_OPTS="${JAVA_OPTS} -XX:+UseG1GC"
JAVA_OPTS="${JAVA_OPTS} -XX:+ParallelRefProcEnabled"
JAVA_OPTS="${JAVA_OPTS} -XX:+DisableExplicitGC"
JAVA_OPTS="${JAVA_OPTS} -Djava.security.egd=file:/dev/./urandom"
JAVA_OPTS="${JAVA_OPTS} -Dlog4j2.formatMsgNoLookups=true"
JAVA_OPTS="${JAVA_OPTS} -Dfile.encoding='utf-8'"
export UMASK=0022
```

`UMASK=0022` matters beyond tidiness: the OCR pipeline derives its output file
mode from the umask of the process that writes it, so a restrictive umask here
produces ALTO that correctors cannot read.

**`org.apache.catalina.core.SecurityListener` no longer exists** in Tomcat 10.
Left in `/etc/tomcat10/server.xml` it throws `ClassNotFoundException` and
Tomcat exits with "Cannot start server. Server instance is not configured."
Verify with `grep -c SecurityListener /etc/tomcat10/server.xml` → must be `0`.

A truncated download shows up as `java.io.EOFException: Expected:200, read:198`
from `JarFile.getManifest` — re-fetch `goobi.war` and the plugin JARs rather
than debugging the stack trace.

## 2. `goobi_config.properties` arrives duplicated

Every line twice, comments included, when the installer's config step runs
more than once. Deduplicate order-preservingly:

```sh
sudo cp goobi_config.properties goobi_config.properties.bak
sudo awk '!seen[$0]++' goobi_config.properties.bak | sudo tee goobi_config.properties >/dev/null
```

Check `historyImageSuffix` afterwards — the duplicate-removal leaves both the
`.jp2` and `.tif` lines, and the last one wins.

## 3. LDAP: the two failures that look like one

**Authentication (Goobi → LDAP bind) and name resolution (NSS → Unix
accounts) are independent.** Goobi logins work with no NSS at all. Only
filesystem operations need Unix accounts: the Read-images home symlink,
chowning files to a user, and Samba shares. A browser-only team needs none of
it.

**Directory build order.** `ldapadd` of the test users fails with
`Undefined attribute type (17): sambaPrimaryGroupSID` until the Samba schema
is loaded, then with `No such object (32)` until the OUs exist. Load
`install/ldap/samba.ldif`, then create `ou=goobi`, `ou=users`, `ou=groups`
and `cn=NextFreeUnixId`, then import users.

**NTLM hashes need the legacy provider.** MD4 is not enabled by default in
OpenSSL 3:

```sh
printf "%s" "$PW" | iconv -t UTF-16LE \
  | openssl dgst -md4 -provider legacy -provider default | awk '{print toupper($NF)}'
```

**`nslcd.conf` is not configured by the installer.** On this host the package
default pointed at the campus Active Directory tree
(`base dc=BoilerAD,dc=Purdue,dc=edu`), so `nslcd` searched a tree that does
not exist locally and every lookup failed silently — `nslcd` active,
`nsswitch.conf` correct, `slapd` serving valid `posixAccount` entries, and
still `id testadmin` → *no such user*. Required values for a local directory:

```
uri  ldap://localhost:389/
base dc=goobi,dc=local
```

Then `systemctl restart nslcd` and confirm `id testadmin` resolves.

> **On a campus VM, reconsider this entirely.** That stale `dc=BoilerAD,dc=Purdue,dc=edu`
> is a hint worth taking: authenticating Goobi against campus AD removes local
> user management altogether. Decide before building a local OpenLDAP you will
> have to migrate off.

**Home directories.** The installer's `chown $u:tomcat` runs before the
accounts resolve and is written `|| true`, so it fails silently and leaves
them `root:root` — after which Read-images fails with
`ln: failed to create symbolic link: Permission denied`. Run it *after* NSS
works:

```sh
for u in testadmin testscanning testmetadata testimaging testqc testprojectmanagement; do
    sudo chown "$u":tomcat "/home/$u" && sudo chmod 775 "/home/$u"
done
```

Target state: `drwxrwxr-x <user> tomcat`.

## 4. Workflow configuration that blocks process creation

Not documented anywhere obvious, and each one presents as a dead end:

- **Every task needs a user group or user** — automatic and script steps
  included (`Process.getContainsUnreachableSteps`). Without one, creating a
  process fails with *"No user associated for task X"* and the create button
  is disabled.
- **The first task must be status Open in the template.** Process creation
  copies task status verbatim and opens nothing
  (`ProzesskopieForm` only queues steps *already* OPEN). A template whose
  first task is Locked yields processes with nothing runnable and an empty
  task list. Later tasks stay Locked; closing task N opens N+1.
- **Leave Read images off on script steps.** It exists so a *human* gets a
  filesystem symlink into their home; a script step receives its paths as
  arguments and runs as `tomcat`. With it on, Goobi passes
  `UserForImageReading` (default `root`), logs *"setting to read-only"*, and
  runs `chmod -R g-w` on the images folder.
- **`images/<processtitle>_media` is not created.** `getImagesTifDirectory`
  only creates it when `useOrigFolder` is false, and it defaults true — so
  `_master` appears and `_media`, which `{tifpath}` points at, does not.
  Either create it per process, or point the script at `{origpath}`.
- **An automatic order-1 step always fires before images can exist.**
  Process creation queues every task that is Open *and* automatic
  immediately (`ProzesskopieForm` starts a `ScriptThreadWithoutHibernate`
  per such step), and `_master` is created empty in that same instant —
  images copied in afterward can never win the race. Observed: the OCR step
  fired at creation, failed with *"No TIFs or PDFs found"*, landed in ERROR
  (which also sends an error mail where mail is configured), and needed a
  manual retry — strictly worse than not being automatic. Automation is
  only sound from order 2 onward: make image delivery a real manual order-1
  task ("Image import" — whoever copies the scans closes it), with OCR
  automatic at order 2. Closing step N is what opens and queues step N+1,
  so OCR then fires exactly when images are guaranteed present, and the
  workflow gains an audit trail of who delivered images and when. (Exit
  codes 98/99 from a step script are the softer tool here: Goobi neither
  closes nor errors the step on those — but nothing re-fires it either, so
  they complement the import step rather than replace it.)

## 5. Target workflow: digitization stages as Goobi tasks

Goobi is meant to be the only place progress is tracked — the spreadsheet's
per-stage *date* and *by* columns are what Goobi records automatically on
every step close (editor + timestamp), so the sheet becomes redundant rather
than something to keep in parallel.

| stage | Goobi mechanism | state |
|---|---|---|
| 0. Create processes from Ref-IDs | `plugin-import-excel` — mass-creates processes from spreadsheet rows | not installed |
| 1. Capture | manual task; images arrive by upload or direct export (below) | ready |
| 2. QC1 (+ image count) | manual task; Goobi counts images itself (`sortHelperImages`) — do not re-enter counts by hand | ready |
| 3. Post-Process | manual check-off; the work itself happens in Capture One | ready |
| 4. Import images | **automatic** script step — `import_images.sh`, share → `_master` | ready |
| 5. QC2 (view images in Goobi) | `plugin-step-imageqa` — browser image inspection inside the task | **installed + configured** |
| 6. OCR | automatic script step (this bridge) | ready |
| 7. Transcript correction | Goobi's ALTO editor and/or this tool's review GUI | ready |
| 8. Review to Upload | manual task + script/export step copying `alto_ocr/<RefID>/` to the LibNova ingest point | small variant of this bridge |
| 9. ReviewToRemove (~1 month) | `plugin-step-delay` — holds a step N days, then auto-closes into a cleanup step | not installed |

OCR is split from correction because an automatic step needs a clean close
boundary — one task cannot both run unattended and be a human review.


**Automation is only sound from order 2 onward** — see the create-time race
above. Stage 1 must be a manual task precisely so that closing it is what
releases OCR.

### Image import without a terminal

Three paths; copying files by hand is none of them.

**Automatic script step (`import_images.sh`) — the primary path.** The share
stages post-processed objects one folder per Ref ID under
`04_ImportImages/<RefID>/`, and the process title *is* the Ref ID, so the
step needs no arguments beyond what Goobi already substitutes. Registered as
*Import Images* in `goobi_scripts.xml`; `GOOBI_IMPORT_ROOT` overrides the
share path. Measured: 30 pages / 1.4 GB in 1m39s over SMB.

Design points worth keeping if it is ever rewritten:

- **rsync `--checksum`, not size-and-time.** SMB timestamps are unreliable
  enough that a truncated earlier transfer can look complete by size and
  mtime alone. These are preservation copies; pay the read.
- **Copies, never moves.** The share's own `09_ReviewToRemove` stage is where
  deletion belongs. Moving here would destroy the only copy before anyone
  has reviewed the results.
- **An empty import root is treated as "share not mounted", not "no images".**
  The two are indistinguishable to `ls`, and importing zero files
  "successfully" would send an empty object on to QC2 and OCR.
- **Nested page files are refused, not flattened.** Goobi lists the master
  folder flat, so a page in a subfolder is invisible to QC2 and the metadata
  editor — but flattening could silently collide two pages sharing a filename
  in different subfolders. Fail and let a human decide.

**Browser upload.** `plugin-step-file-upload` is installed and its shipped
config already has a catch-all block (any project, any step, TIFF/JP2/PDF
accepted, target folder `master`). Set **Step plugin** = `intranda_step_fileUpload`
on the Capture task and its detail view gains an upload box writing straight
into `_master`. Works today; clunky for multi-GB objects.

**Direct export from the capture station — the pattern the LDAP/Samba stack
exists for.** Give the Capture task **Write images** (not merely Read images:
the flag is inverted into `nurLesen`, and read-only mode passes
`UserForImageReading`, default `root`). Accepting the task then symlinks the
process folder into the digitizer's home; export those homes over Samba, and
Capture One's output recipe writes directly into `_master` through the share.
The export *is* the import — no copy step exists to forget. Requires NSS
working and homes owned `<user>:tomcat` (§3), and the imaging stations
needing SMB access to the Goobi server belongs in the VM request.

### Where correction happens

Both editors write the same `_alto` files, and both are legitimate:

- **Goobi's built-in ALTO editor** (metadata editor, `showOcrButton` /
  `loadJsonAlto`) — no extra software, correctors never leave Goobi.
- **This tool's review GUI** — confidence ranking, worst-first queue, add/
  delete word, reading-order mode. Needs `python3-tk` (§7).

Goobi's editor writes `CONTENT` and nothing else: no provenance element, and
`WC` left at its original value. The re-OCR guard therefore relies on METS
checksum drift to notice those edits, and `repackage` converts drift into a
durable stamp (alto_ocr ≥ v0.8.17). Two consequences worth knowing: an object
OCR'd with `--no-mets` or `--no-checksums` has no fingerprint and so no
protection for Goobi-side edits, and a page corrected in Goobi keeps its
original confidence, so it still surfaces in the worst-first triage queue.

## 6. OCR settings that matter more than they look

**`--psm 1`, not Tesseract's `psm 3` default.** Both do fully automatic page
segmentation; only `psm 1` runs orientation and script detection. A master TIFF
scanned sideways, read under `psm 3`, yields vertically-stacked words at
`WC 0.00` — and nothing in the output says "orientation": it reads as an image
quality problem, and a corrector faces hundreds of flagged words on a page
whose text was perfectly legible all along. Measured over one 54-page object of
typed reports:

| | psm 3 | psm 1 |
|---|---|---|
| mean WC | 0.869 | **0.922** |
| low-confidence words | 1199 (9.9%) | **489 (4.1%)** |
| worst page PC | 0.32 | 0.52 |

Every page gained, not only the rotated one, and the word count barely moved
(12095 → 11987) — it is reading the same text, correctly. The wrapper defaults
to `psm 1`; `ALTO_OCR_PSM` overrides it.

Note OSD corrects the *text* while leaving the ALTO *geometry* in the
master's frame: a rotated page's word boxes stay taller-than-wide because the
words genuinely are sideways in the master image. That is the correct
behavior, by policy and by design — preservation masters are never modified,
rotation exists only for transcription accuracy, and ALTO coordinates must
map onto the master as captured (the same coordinate-frame contract that
keeps deskew and border-crop disabled). Do not "fix" this by rotating pixels
in preprocessing: that shifts every box off its word for all downstream
consumers. Expect vertical boxes over sideways pages in the review GUI; the
boxes are on the words.

**Low mean WC does not mean "hard text".** It can equally mean *no* text:
photographic material scores badly because the engine finds noise. One object
of postcards scored 0.587 and tripped the tool's "handwriting candidate"
hint, where the correct action was to not OCR it at all. Check words-per-page
before acting on a low score — 36 words on a full page is a photograph, not a
handwriting problem.

## 7. Carrying workflow config to another host

**Goobi has no template export.** The template page offers only "new" and
"edit"; the REST API creates processes *from* a template, never a template.
Templates live in MariaDB, so SQL is the only way to move a workflow
definition — or to keep one under version control.

`export_template.sh` emits the minimal re-importable dump: the template row,
its steps, and each step's user-group assignments. Nothing more is needed,
because everything a template references — institution, project, ruleset,
docket, the six default user groups — is stock seed data present with
**identical IDs** on any fresh install. Verified against this schema: a fresh
install seeds `prozesse` 1 and 4 and `schritte` up to 28, so a template built
later (here: process 6, steps 36/38/45) cannot collide.

```sh
integration/goobi/export_template.sh OCR_Processing > OCR_Processing.sql
# on the target, after running the pre-flight queries in the file's header:
mysql goobi < OCR_Processing.sql && systemctl restart tomcat10
```

**The schema declares no foreign keys at all** (checked: zero `FOREIGN KEY`
in `goobi_blank.sql`, everything InnoDB). So an import referencing a project
or user group the target lacks succeeds silently and produces a template that
looks correct and then fails at process creation with *"No user associated
for task X"*. That is why the script emits per-dependency pre-flight queries
into the dump's header rather than a generic warning — run them.

Credentials come from Tomcat's context XML rather than the install's
credentials file, which drifts the first time anyone rotates the password
(observed: the file's `PW_SQL_GOOBI` no longer authenticates on this host).

Not exported, deliberately: users, processes, history, process logs. User
accounts especially should be created on the target from its own directory —
see the AD decision in §3 — rather than migrated from a test host.

For one template, rebuilding by hand in the UI is comparable effort and
carries no ID risk at all. The dump's real value is as a versioned artifact:
a workflow definition that can be reviewed in a diff and restored after a
mistake.

## 8. Mounts must exist before Tomcat starts

`tomcat10.service` ships with `PrivateTmp=yes`, which gives the service its own
mount namespace. A share mounted *after* Tomcat started is therefore invisible
to Goobi while a login shell sees it perfectly:

```
tomcat's namespace:   /mnt/wsl  /mnt/wslg  /mnt/c
a login shell:        /mnt/wsl  /mnt/wslg  /mnt/c  /mnt/l  /mnt/y
```

Observed live: the image-import step failed with "import root does not exist"
twice while `ls` on the same path succeeded interactively. `systemctl restart
tomcat10` after mounting fixes it, and the import step now distinguishes this
case explicitly instead of blaming the path — it walks up to the deepest
existing ancestor and reports an *empty* directory where a share should be,
which is the shared signature of "not mounted" and "mounted in another
namespace". (It probes by walking the path rather than reading
`/proc/1/mountinfo`, which an unprivileged service cannot do.)

**On the VM this is a design constraint, not a WSL quirk.** Mount the
digitization share via `/etc/fstab` with `_netdev`, and add
`RequiresMountsFor=/path/to/share` to the Tomcat unit as a drop-in under
`/etc/systemd/system/tomcat10.service.d/`, so systemd orders the two and
re-establishes them together. Otherwise a network blip that remounts the share
leaves Goobi unable to see it until someone restarts Tomcat, with no error that
points at the cause.

## 9. Host suitability

WSL2 is fine for building and evaluating, and unfit for shared use:

- `eth0` sits on a NAT'd address that changes on restart (`172.22.x.x` here),
  so nothing outside the Windows host can reach Apache regardless of what it
  binds to.
- No survival across logout or reboot without `wsl.conf` and scheduled-task
  work.
- Samba out of WSL2 to other machines is its own project.

The moment a second person needs a browser on the instance, it needs a real
VM. Nothing else in this document changes when it moves — the fixes above
apply unaltered.

## 10. The OCR bridge

See `README.md` in this directory. Host requirements it adds:

- Tesseract on `PATH` (`tesseract 5.3.4` here) and its language data.
- The application and its venv readable **and executable by `tomcat`** — the
  usual trip hazard when the app lives under `/home`. Verify with
  `sudo -u tomcat <app>/venv/bin/python -c "import alto_ocr"`, not by running
  it as yourself.
- `python3-tk` from apt if correctors will run the review GUI on this host.
  tkinter is not a pip package and is absent from a stock venv; without it the
  GUI cannot start at all. Under WSL, WSLg supplies the display (`DISPLAY=:0`).
- `install.sh` in this directory places the wrapper and its registration.

**Deploy a pinned checkout, never a development working tree.** The wrapper
executes `<app-dir>/src` directly, so whatever the working tree currently
holds *is* what Goobi runs — a `git checkout` on that directory changes
production instantly and silently, with no restart and no log entry. This bit
during testing: switching branches to commit a doc reverted a bug fix
underneath a running Goobi, and the resulting behaviour looked convincingly
like a second bug. Deploy at a tag or merge commit and update it deliberately.

**Correctors need group membership, not permission changes.** Saving a
correction replaces the ALTO through the symlink view, which needs write on
the *directory* (owned by `tomcat`, mode 775) rather than on the file — so
adding the corrector to the `tomcat` group is sufficient, and no chmod of the
output is required. Modes take care of themselves: the pipeline preserves an
existing file's mode across writes and OCR always creates the file first as
`tomcat`, so a corrector with even a `0077` umask cannot produce output Goobi
is unable to read (verified). Setgid on the OCR directories is optional
tidiness for consistent group ownership, not a requirement.
