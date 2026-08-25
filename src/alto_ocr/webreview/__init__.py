"""Browser-based transcript review — the multi-user face of ``remediate.py``.

The Tkinter Review tab (``review.py``) and this package are two presentation
layers over the *same* headless domain logic in ``remediate.py``: identical
edit/verify/delete/add semantics, identical provenance stamping, identical
canonical transcript rendering. Nothing about how a correction is recorded
lives here, on purpose — a rule that exists in two places drifts.

Why a browser layer at all (decided 2026-08-25): Goobi workflow is the sole
interface for tracking digitization progress, and its own ALTO editor cannot
be used for correction — it writes ``CONTENT`` only (``AltoSaver``), never
touches ``WC``, so corrected words keep their engine confidence and never
leave the review queue, and it reads ALTO coordinates without honouring
``MeasurementUnit``, so on the institution's mm10 ALTO every highlight box
lands 1.968x small toward the top-left. A task-linked page beside Goobi
gives correctors one click from the task they are already standing in, with
no install on their workstation and no Tk display on the server.

Layout:

* ``config.py``    — where the ALTO, the scans and the cache live.
* ``discovery.py`` — which objects exist and what state they are in.
* ``imaging.py``   — TIF scans the browser cannot render -> cached JPEG.
* ``ops.py``       — apply a batch of corrections to one page and save it.
* ``app.py``       — thin FastAPI wiring over the four modules above.

``config``, ``discovery``, ``imaging`` and ``ops`` import no web framework
and are unit-tested without a server, exactly as ``remediate`` is tested
without a display.
"""

from .config import ReviewServerConfig
from .discovery import ReviewObject, ObjectIndex

__all__ = ["ReviewServerConfig", "ReviewObject", "ObjectIndex"]
