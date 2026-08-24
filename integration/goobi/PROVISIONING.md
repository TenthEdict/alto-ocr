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

## 5. OCR settings that matter more than they look

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

Note OSD fixes the *text* but not the ALTO *geometry*: a rotated page's word
boxes stay taller-than-wide, so the review GUI draws vertical boxes over
horizontal text. The transcript is right and clicking is awkward. The real fix
is upstream — a preservation master should not be sideways, and every
downstream consumer pays for it, not just OCR.

**Low mean WC does not mean "hard text".** It can equally mean *no* text:
photographic material scores badly because the engine finds noise. One object
of postcards scored 0.587 and tripped the tool's "handwriting candidate"
hint, where the correct action was to not OCR it at all. Check words-per-page
before acting on a low score — 36 words on a full page is a photograph, not a
handwriting problem.

## 6. Host suitability

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

## 7. The OCR bridge

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
