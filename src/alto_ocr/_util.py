"""Small cross-cutting helpers shared by the CLI and the QA scripts.

Kept dependency-free (stdlib only) so a script can import it without pulling
in typer/rich/opencv. Holds the runtime helpers (``force_utf8_output``, the
``atomic_write_*`` file writers).
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# os.replace onto an existing file intermittently fails on Windows/SMB shares
# when something else briefly holds a handle on the destination — an antivirus
# scanner, the Search indexer, or an oplock break still in flight. Windows
# surfaces that as ERROR_ACCESS_DENIED (5) as often as ERROR_SHARING_VIOLATION
# (32), so the errno alone can't distinguish it from a real permission problem;
# the retry does, since a genuine ACL denial fails every attempt. Observed
# 2026-07-30 writing ALTO to \\wl.mydata.purdue.edu (3 of 41 pages in one
# object, while pages written seconds either side succeeded).
_REPLACE_ATTEMPTS = 6
_REPLACE_BACKOFF_S = 0.05
_TRANSIENT_WINERRORS = frozenset({5, 32})


def _replace_with_retry(tmp_name: str, path: Path) -> None:
    """``os.replace`` with bounded backoff for transient Windows/SMB failures.

    Retries only errors that a concurrent handle can plausibly cause. A real
    permission problem still raises — it just takes ~1.5s of retries first,
    which is nothing next to an OCR pass and buys robustness on network drives.
    """
    delay = _REPLACE_BACKOFF_S
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(tmp_name, path)
            if attempt > 1:
                logger.debug("replace of %s succeeded on attempt %d", path.name, attempt)
            return
        except OSError as e:
            transient = getattr(e, "winerror", None) in _TRANSIENT_WINERRORS
            if not transient or attempt == _REPLACE_ATTEMPTS:
                raise
            time.sleep(delay)
            delay *= 2


def force_utf8_output() -> None:
    """Make stdout/stderr tolerate non-ASCII output on Windows consoles.

    Rich tables/log lines and OCR text emit characters (``→``, ``←``, em
    dashes, ½, CJK) that the legacy Windows cp1252 console codec can't encode,
    which otherwise crashes at print time (observed under redirected output).
    Reconfigure the streams to UTF-8 with a lenient error handler, and on
    Windows switch the console output code page to UTF-8 so renderers that
    encode via the code page (not the Python stream) succeed too.

    All best-effort: every step is guarded so a locked-down environment can't
    stop the tool from running. No-op on POSIX, where stdout is already UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    if sys.platform == "win32":
        try:
            import ctypes

            # 65001 = CP_UTF8. Output code page only; leave the input code page
            # alone so console input handling is unaffected.
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # best effort; never fatal
            pass


# Probed once, at import, while the process is still single-threaded: reading
# the umask means temporarily setting it, which would be a race if two threads
# (or a forked pool worker mid-run) did it concurrently. On Windows umask is
# effectively 0 and chmod only toggles the read-only bit, so this is a no-op
# there.
_UMASK = os.umask(0o022)
os.umask(_UMASK)


def _target_mode(path: Path) -> int:
    """The mode an atomic write should leave on ``path``.

    ``tempfile.mkstemp`` hard-codes 0600 and ``os.replace`` preserves the temp
    file's mode, so without this every atomically-written file comes out
    private to the writing user. That breaks any workflow where one account
    produces output and another consumes it — Goobi workflow runs the pipeline
    as ``tomcat`` while correctors open the same ALTO as themselves.

    An existing destination keeps its own mode, so a deliberate ``chmod`` on
    output survives a re-run. Otherwise fall back to what an ordinary
    ``open(path, "w")`` would have produced under this process's umask.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return 0o666 & ~_UMASK


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file + ``os.replace``).

    The destination is never opened for writing, so a crash, disk-full, or
    network hiccup mid-write cannot truncate an existing file: readers see
    either the old complete content or the new complete content. That is
    load-bearing for hand-corrected ALTO, where a truncated file destroys
    human labor. The temp file lives in the destination's directory so the
    final rename never crosses filesystems. The parent directory must exist.

    The final rename retries briefly (see ``_replace_with_retry``) because on
    SMB shares it intermittently fails with a transient access-denied.
    """
    path = Path(path)
    mode = _target_mode(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with open(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # best effort — some network filesystems refuse fsync
        # Before the rename, so the file is never briefly visible at the
        # destination with the wrong mode. Best effort: a filesystem that
        # refuses chmod (some SMB/NFS mounts) must not fail the write.
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            logger.debug("chmod %o failed on %s; leaving mkstemp's mode",
                         mode, tmp_name)
        _replace_with_retry(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> None:
    """Atomic drop-in for ``Path.write_text`` (see ``atomic_write_bytes``).

    ``newline=None`` (the default) applies the same platform translation text
    mode would (``\\n`` -> ``os.linesep``), so a successful write produces
    byte-identical output to the ``write_text`` call it replaces. Encoding
    happens before any file is touched, so an encode error leaves the
    destination untouched and creates no temp file.
    """
    if newline is None:
        text = text.replace("\n", os.linesep)
    elif newline:
        text = text.replace("\n", newline)
    atomic_write_bytes(path, text.encode(encoding))
