"""Reusable Tk pieces for the desktop app.

* ``Detachable`` — lets a frame leave its place in a window, become a
  window of its own, and come back. The Review tab uses it for the page
  (more screen for corrections — a second monitor, a maximized window)
  and for the symbol keyboard.
* ``SymbolKeyboard`` — renders ``symbols.SYMBOL_KEYBOARD`` as rows of keys
  under category tabs. The layout and its ALTO rules live in the headless
  ``symbols`` module, shared with the web review client; this is only the
  Tk face of it.
"""

from __future__ import annotations

import re
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable, Optional

from . import theme
from .symbols import SYMBOL_KEYBOARD, SymbolCategory, symbol_label

_GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$")


def parse_geometry(geometry: str) -> Optional[tuple[int, int, int, int]]:
    """``"WxH+X+Y"`` → (w, h, x, y); None for anything else. Tk writes an
    off-left position as ``+-1200``, which this reads as x = -1200."""
    m = _GEOMETRY_RE.match(geometry.strip()) if isinstance(geometry, str) else None
    if m is None:
        return None
    w, h, x, y = m.groups()
    return int(w), int(h), int(x.lstrip("+")), int(y.lstrip("+"))


def _point_on_a_screen(widget: tk.Misc, x: int, y: int) -> bool:
    """Is screen point (x, y) on any monitor? On Windows asks the OS, which
    knows about every monitor; elsewhere the X root window spans them."""
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt
            return bool(ctypes.windll.user32.MonitorFromPoint(wt.POINT(x, y), 0))
        except (AttributeError, OSError):  # pragma: no cover - defensive
            pass
    return (0 <= x < widget.winfo_screenwidth()
            and 0 <= y < widget.winfo_screenheight())


class Detachable:
    """Moves a classic ``tk.Frame`` between its place in a window and a
    window of its own, without rebuilding anything.

    Built on Tk's ``wm manage`` / ``wm forget``: the frame and every widget
    in it keep their identity, bindings, variables and state, so a
    half-corrected page moves between windows untouched, undo stack and all.
    Only classic frames can be managed (``ttk.Frame`` raises TclError).

    The frame must be gridded in its home position; ``detach`` remembers
    the placement and ``attach`` restores it. While detached, an optional
    ``placeholder`` fills the same grid cell. Closing the window attaches
    the frame again rather than destroying it.

    The window's geometry is kept in ``settings[geometry_key]`` (written
    through ``persist``), so a page parked on a second monitor reopens
    there. A remembered position that is on no screen any more (monitor
    unplugged, laptop undocked) is ignored. ``default_size=None`` gives a
    window that sizes itself to its content and remembers only where it was.
    ``owner`` (optional) keeps the window above another one, the way a tool
    palette stays above the document it works on.
    """

    def __init__(
        self,
        frame: tk.Frame,
        *,
        title: str,
        placeholder: Optional[tk.Widget] = None,
        settings: Optional[dict] = None,
        geometry_key: Optional[str] = None,
        persist: Optional[Callable[[], None]] = None,
        default_size: Optional[tuple[int, int]] = (1000, 800),
        owner: Optional[Callable[[], tk.Misc]] = None,
        on_change: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.frame = frame
        self.placeholder = placeholder
        self._title = title
        self._settings = settings
        self._key = geometry_key
        self._persist = persist
        self._default_size = default_size
        self._owner = owner
        self._on_change = on_change
        self._placement: dict = {}
        self._detached = False

    @property
    def detached(self) -> bool:
        return self._detached

    def set_title(self, title: str) -> None:
        self._title = title
        if self._detached:
            tk.Wm.wm_title(self.frame, title)

    def toggle(self) -> None:
        if self._detached:
            self.attach()
        else:
            self.detach()

    def detach(self) -> None:
        if self._detached:
            return
        frame = self.frame
        self._placement = frame.grid_info()
        if not self._placement:
            raise RuntimeError(f"{frame} must be gridded before it can be detached")
        if self._default_size is None:
            # A palette lifts out exactly where it sat.
            home_xy = (frame.winfo_rootx(), frame.winfo_rooty())
        else:
            # A big window opens just inside the one it came from.
            home = frame.winfo_toplevel()
            home_xy = (home.winfo_rootx() + 40, home.winfo_rooty() + 20)
        frame.grid_forget()
        frame.tk.call("wm", "manage", frame._w)
        self._detached = True
        # Unbound Wm methods work on a managed frame: it is a toplevel now.
        tk.Wm.wm_title(frame, self._title)
        tk.Wm.wm_protocol(frame, "WM_DELETE_WINDOW", self.attach)
        theme.title_bar(frame)  # a dark title bar in the dark theme
        self._apply_owner()
        self._restore_geometry(home_xy)
        if self.placeholder is not None:
            self.placeholder.grid(**self._placement)
        if self._on_change is not None:
            self._on_change(True)

    def attach(self) -> None:
        if not self._detached:
            return
        self.remember_geometry()
        frame = self.frame
        frame.tk.call("wm", "forget", frame._w)
        self._detached = False
        if self.placeholder is not None:
            self.placeholder.grid_remove()
        frame.grid(**self._placement)
        if self._on_change is not None:
            self._on_change(False)

    def reown(self) -> None:
        """Re-apply the owner window — call after the owner itself moved
        between windows (the keyboard follows the page it types into)."""
        if self._detached:
            self._apply_owner()

    def remember_geometry(self) -> None:
        """Store the window's normal geometry (and whether it is maximized)."""
        if not self._detached or self._settings is None or not self._key:
            return
        frame = self.frame
        try:
            state = tk.Wm.wm_state(frame)
            geometry = tk.Wm.wm_geometry(frame)
        except tk.TclError:
            return
        saved = dict(self._settings.get(self._key) or {})
        if state == "normal":
            saved["geometry"] = geometry
        # A maximized window's geometry is the whole screen; keep the last
        # normal one and just remember to maximize again.
        saved["zoomed"] = state == "zoomed"
        self._settings[self._key] = saved
        if self._persist is not None:
            self._persist()

    # ---------- internals ------------------------------------------------

    def _apply_owner(self) -> None:
        if self._owner is None:
            return
        owner = self._owner()
        if owner is not None and str(owner) != str(self.frame):
            tk.Wm.wm_transient(self.frame, owner)

    def _restore_geometry(self, home_xy: tuple[int, int]) -> None:
        frame = self.frame
        saved = (self._settings or {}).get(self._key) if self._key else None
        saved = saved if isinstance(saved, dict) else {}
        parsed = parse_geometry(saved.get("geometry", ""))
        if parsed is not None:
            w, h, x, y = parsed
            # The title bar must be reachable, or the window cannot be moved.
            if _point_on_a_screen(frame, x + min(w, 200) // 2, y + 10):
                size = f"{w}x{h}" if self._default_size is not None else ""
                tk.Wm.wm_geometry(frame, f"{size}+{x}+{y}")
                if saved.get("zoomed") and self._default_size is not None:
                    self._zoom()
                return
        # Somewhere on the monitor it came from (see detach).
        x, y = home_xy
        if self._default_size is None:
            tk.Wm.wm_geometry(frame, f"+{x}+{y}")
            return
        w = min(self._default_size[0], int(frame.winfo_screenwidth() * 0.9))
        h = min(self._default_size[1], int(frame.winfo_screenheight() * 0.85))
        tk.Wm.wm_geometry(frame, f"{w}x{h}+{x}+{y}")

    def _zoom(self) -> None:
        try:
            tk.Wm.wm_state(self.frame, "zoomed")
        except tk.TclError:  # X11 window managers without "zoomed"
            try:
                tk.Wm.wm_attributes(self.frame, "-zoomed", True)
            except tk.TclError:
                pass


class Tooltip:
    """A small hint that appears when the pointer rests on a widget, and
    goes away when it leaves or clicks."""

    DELAY_MS = 450

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self._pending: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: object = None) -> None:
        self._hide()
        self._pending = self.widget.after(self.DELAY_MS, self._show)

    def _show(self) -> None:
        self._pending = None
        if self._tip is not None or not self.widget.winfo_viewable():
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{self.widget.winfo_rootx() + 6}"
                        f"+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")
        pal = theme.current()
        tk.Label(tip, text=self.text, background=pal.tip_bg, foreground=pal.tip_fg,
                 relief="solid", borderwidth=1, padx=5, pady=2).pack()
        self._tip = tip

    def _hide(self, _event: object = None) -> None:
        if self._pending is not None:
            self.widget.after_cancel(self._pending)
            self._pending = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class SymbolKeyboard(ttk.Frame):
    """Rows of keys under category tabs. Clicking a key calls
    ``on_symbol(ch)``; resting the pointer on one shows its code point and
    Unicode name — how a corrector tells μ from a look-alike.

    Keys never take keyboard focus, so a click leaves the caret wherever the
    corrector was typing. Only the chosen category's keys are laid out, so
    the keyboard is as tall as that category (one row for Common) rather
    than as tall as the biggest one. ``header`` (optional) adds controls at
    the right end of the tab strip — the Review tab's pop-out button.
    """

    def __init__(
        self,
        master: tk.Misc,
        on_symbol: Callable[[str], None],
        *,
        layout: tuple[SymbolCategory, ...] = SYMBOL_KEYBOARD,
        header: Optional[Callable[[ttk.Frame], None]] = None,
        initial: str = "",
        on_category: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(master)
        self._on_symbol = on_symbol
        self._on_category = None  # not for the initial category — set below
        self.keys: dict[str, list[tk.Button]] = {}
        self._pages: dict[str, ttk.Frame] = {}
        names = [c.name for c in layout]
        self._category = tk.StringVar(
            value=initial if initial in names else (names[0] if names else ""))

        family = tkfont.nametofont("TkDefaultFont").actual("family")
        self._font = tkfont.Font(self, family=family, size=12)

        strip = ttk.Frame(self)
        strip.pack(fill="x")
        for category in layout:
            ttk.Radiobutton(strip, text=category.name, value=category.name,
                            variable=self._category, style="Toolbutton",
                            takefocus=False, command=self._show_category,
                            ).pack(side="left")
        if header is not None:
            header(strip)

        for category in layout:
            page = ttk.Frame(self, padding=(0, 2, 0, 0))
            for r, row in enumerate(category.rows):
                for c, ch in enumerate(row):
                    self._add_key(page, ch, r, c)
            self._pages[category.name] = page
        self._shown: Optional[ttk.Frame] = None
        self._show_category()
        self._on_category = on_category

    def _add_key(self, parent: ttk.Frame, ch: str, row: int, col: int) -> None:
        # Classic buttons: raised like keys, compact, and never focusable.
        pal = theme.current()
        key = tk.Button(parent, text=ch, font=self._font, width=2, padx=0, pady=0,
                        relief="raised", borderwidth=1, takefocus=0,
                        background=pal.button, foreground=pal.fg,
                        activebackground=pal.button_active, activeforeground=pal.fg,
                        command=lambda c=ch: self._on_symbol(c))
        key.grid(row=row, column=col, padx=1, pady=1, sticky="nsew")
        Tooltip(key, symbol_label(ch))
        self.keys.setdefault(ch, []).append(key)

    def _show_category(self) -> None:
        page = self._pages.get(self._category.get())
        if page is None or page is self._shown:
            return
        if self._shown is not None:
            self._shown.pack_forget()
        page.pack(anchor="w")
        self._shown = page
        if self._on_category is not None:
            self._on_category(self._category.get())

    def current_category(self) -> str:
        return self._category.get()

    def select_category(self, name: str) -> None:
        if name in self._pages:
            self._category.set(name)
            self._show_category()
