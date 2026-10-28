"""Light and dark appearance for the desktop app.

**View ▸ Theme** offers Light (the native Windows look, the default), Dark,
and Match Windows (follows the "app mode" in Windows' colour settings,
re-checked whenever the app window is activated). The choice is saved as
``theme`` in the settings file.

Tk has two kinds of widget, and they theme differently:

* **ttk widgets** take their colours from the active ttk theme. Light keeps
  the native theme (``vista`` on Windows), which ignores colour options;
  Dark switches to ``clam``, the one built-in theme that honours them, and
  configures its styles. ``style.configure`` is per theme, so switching
  back to the native theme drops every dark setting at once.
* **Classic widgets** (the page list, the transcript panel, the scan
  canvas, the symbol keys, menus, classic frames) are recoloured one by one
  by walking the widget tree.

Hint text uses the ``Muted.TLabel`` style and the Run tab's settings link
``Link.TLabel``, configured for both palettes, instead of fixed greys that
vanish on a dark background. The run log keeps its own dark colours in both
themes (``theme_exempt``). On Windows the title bars follow too, through the
DWM immersive-dark-mode attribute (Windows 10 2004 and later; a no-op where
unsupported).
"""

from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

MODES = ("light", "dark", "system")
MODE_LABELS = {"light": "Light", "dark": "Dark", "system": "Match Windows"}
DEFAULT_MODE = "light"


@dataclass(frozen=True)
class Palette:
    name: str
    dark: bool
    bg: str            # window and frame background
    surface: str       # inactive tabs, menus
    field: str         # entry, list and text backgrounds
    fg: str
    muted: str         # hint text
    link: str
    border: str
    button: str
    button_active: str
    trough: str
    accent: str
    select_bg: str
    select_fg: str
    canvas: str        # around the scan
    chosen_bg: str     # reading-order selection in the transcript panel
    chosen_fg: str
    tip_bg: str
    tip_fg: str


LIGHT = Palette(
    name="light", dark=False,
    bg="#f0f0f0", surface="#f0f0f0", field="#ffffff", fg="#1a1a1a",
    muted="#5f6368", link="#1f6feb", border="#adadad",
    button="#f0f0f0", button_active="#e5f1fb", trough="#e6e6e6",
    accent="#0b5cad", select_bg="#0078d7", select_fg="#ffffff",
    canvas="#404040", chosen_bg="#cfe0ff", chosen_fg="#0b3d91",
    tip_bg="#ffffe1", tip_fg="#222222",
)

DARK = Palette(
    name="dark", dark=True,
    bg="#202124", surface="#2a2b2e", field="#2b2d31", fg="#e6e6e6",
    muted="#a0a4ab", link="#7ab4ff", border="#4a4d52",
    button="#34363a", button_active="#43464b", trough="#1a1b1e",
    accent="#4c9bea", select_bg="#2f5f99", select_fg="#ffffff",
    canvas="#16171a", chosen_bg="#1f3a5f", chosen_fg="#cfe3ff",
    tip_bg="#3a3c40", tip_fg="#f0f0f0",
)

_current: Palette = LIGHT


def current() -> Palette:
    """The palette in effect — for widgets created after a theme change
    (tooltips) that colour themselves."""
    return _current


def normalize_mode(mode: object) -> str:
    return mode if mode in MODES else DEFAULT_MODE


def system_prefers_dark() -> bool:
    """Windows' "Choose your app mode" setting; False where there is none."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except OSError:
        return False


def resolve(mode: str) -> Palette:
    mode = normalize_mode(mode)
    if mode == "system":
        return DARK if system_prefers_dark() else LIGHT
    return DARK if mode == "dark" else LIGHT


def native_theme(style: ttk.Style) -> str:
    """The platform's own ttk theme, used for Light."""
    names = style.theme_names()
    for preferred in ("vista", "winnative", "aqua", "clam", "default"):
        if preferred in names:
            return preferred
    return style.theme_use()


def apply(root: tk.Misc, mode: str) -> Palette:
    """Switch the whole app to ``mode`` and return the palette now in use."""
    global _current
    pal = resolve(mode)
    _current = pal
    style = ttk.Style(root)
    try:
        style.theme_use("clam" if pal.dark else native_theme(style))
    except tk.TclError:
        pass
    if pal.dark:
        _configure_dark(style, pal)
    # Styles every theme needs; configured after theme_use, which is what
    # makes them land on the theme now active.
    style.configure("Muted.TLabel", foreground=pal.muted)
    style.configure("Link.TLabel", foreground=pal.link)
    _option_defaults(root, pal)
    recolor_tree(root, pal)
    return pal


def recolor_tree(widget: tk.Misc, pal: Palette | None = None) -> None:
    """Recolour ``widget`` and everything inside it — for windows built
    after the theme was applied (the Advanced settings window)."""
    pal = pal or _current
    stack = [widget]
    while stack:
        w = stack.pop()
        _recolor(w, pal)
        try:
            stack.extend(w.winfo_children())
        except tk.TclError:
            continue


def title_bar(window: tk.Misc, dark: bool | None = None) -> None:
    """Dark or light Windows title bar for a toplevel (or a frame that
    ``wm manage`` turned into one)."""
    if sys.platform != "win32":
        return
    dark = _current.dark if dark is None else dark
    try:
        import ctypes
        window.update_idletasks()
        hwnd = int(window.tk.call("wm", "frame", window._w), 16)
        value = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE; 19 on Windows 10 before 20H1.
        for attribute in (20, 19):
            if dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                         ctypes.sizeof(value)) == 0:
                break
        # Repaint the frame now rather than at the next resize.
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
    except (AttributeError, OSError, ValueError, tk.TclError):  # pragma: no cover - platform
        pass


# ---------- internals ------------------------------------------------------

def _configure_dark(style: ttk.Style, pal: Palette) -> None:
    style.configure(
        ".", background=pal.bg, foreground=pal.fg, fieldbackground=pal.field,
        bordercolor=pal.border, darkcolor=pal.bg, lightcolor=pal.bg,
        troughcolor=pal.trough, focuscolor=pal.accent, insertcolor=pal.fg,
        selectbackground=pal.select_bg, selectforeground=pal.select_fg,
        arrowcolor=pal.fg)
    style.map(".", foreground=[("disabled", pal.muted)],
              background=[("disabled", pal.bg)])
    for name in ("TButton", "Toolbutton"):
        style.configure(name, background=pal.button, lightcolor=pal.button,
                        darkcolor=pal.button, bordercolor=pal.border)
        style.map(name,
                  background=[("disabled", pal.bg), ("pressed", pal.select_bg),
                              ("selected", pal.select_bg), ("active", pal.button_active)],
                  foreground=[("disabled", pal.muted), ("selected", pal.select_fg)],
                  lightcolor=[("pressed", pal.select_bg), ("selected", pal.select_bg)],
                  darkcolor=[("pressed", pal.select_bg), ("selected", pal.select_bg)])
    style.configure("Toolbutton", background=pal.bg, lightcolor=pal.bg, darkcolor=pal.bg)
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(name, fieldbackground=pal.field, foreground=pal.fg,
                        background=pal.button, insertcolor=pal.fg,
                        lightcolor=pal.field, darkcolor=pal.field, arrowcolor=pal.fg)
        style.map(name,
                  fieldbackground=[("readonly", pal.field), ("disabled", pal.bg)],
                  foreground=[("disabled", pal.muted)],
                  selectbackground=[("readonly", "!focus", pal.field),
                                    ("readonly", "focus", pal.select_bg)],
                  selectforeground=[("readonly", "!focus", pal.fg),
                                    ("readonly", "focus", pal.select_fg)],
                  bordercolor=[("focus", pal.accent)])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=pal.bg, foreground=pal.fg,
                        indicatorbackground=pal.field, indicatorforeground=pal.fg)
        style.map(name, background=[("active", pal.bg)],
                  indicatorbackground=[("pressed", pal.button_active),
                                       ("selected", pal.field)])
    style.configure("TNotebook", background=pal.bg, bordercolor=pal.border,
                    lightcolor=pal.bg, darkcolor=pal.bg)
    style.configure("TNotebook.Tab", background=pal.surface, foreground=pal.muted,
                    bordercolor=pal.border, lightcolor=pal.surface, darkcolor=pal.surface)
    style.map("TNotebook.Tab",
              background=[("selected", pal.bg)], foreground=[("selected", pal.fg)],
              lightcolor=[("selected", pal.bg)], darkcolor=[("selected", pal.bg)])
    style.configure("TLabelframe", background=pal.bg, bordercolor=pal.border,
                    lightcolor=pal.bg, darkcolor=pal.bg)
    style.configure("TLabelframe.Label", background=pal.bg, foreground=pal.fg)
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background=pal.button, troughcolor=pal.trough,
                        bordercolor=pal.border, arrowcolor=pal.fg,
                        lightcolor=pal.button, darkcolor=pal.button)
        style.map(name, background=[("active", pal.button_active)])
    style.configure("Horizontal.TProgressbar", background=pal.accent,
                    troughcolor=pal.trough, bordercolor=pal.border,
                    lightcolor=pal.accent, darkcolor=pal.accent)
    style.configure("TSeparator", background=pal.border)
    style.configure("Sash", background=pal.border, bordercolor=pal.border,
                    lightcolor=pal.border, darkcolor=pal.border, gripcount=0)
    # The dark theme's stand-in for the native menu bar (gui._build_menu):
    # flat buttons with no drop-down arrow.
    style.configure("Menubar.TFrame", background=pal.surface)
    style.layout("Menubar.TMenubutton", [("Menubutton.border", {"sticky": "nswe", "children": [
        ("Menubutton.padding", {"sticky": "nswe", "children": [
            ("Menubutton.label", {"sticky": "nswe"})]})]})])
    style.configure("Menubar.TMenubutton", background=pal.surface, foreground=pal.fg,
                    relief="flat", borderwidth=0, padding=(9, 3), width=0)
    style.map("Menubar.TMenubutton",
              background=[("pressed", pal.select_bg), ("active", pal.button_active)],
              foreground=[("pressed", pal.select_fg)])


def _option_defaults(root: tk.Misc, pal: Palette) -> None:
    """Defaults for classic widgets created later, and for the Combobox
    drop-down list, which Tk builds the first time it opens."""
    for pattern, value in (
        ("*TCombobox*Listbox.background", pal.field),
        ("*TCombobox*Listbox.foreground", pal.fg),
        ("*TCombobox*Listbox.selectBackground", pal.select_bg),
        ("*TCombobox*Listbox.selectForeground", pal.select_fg),
        ("*Toplevel.background", pal.bg),
        ("*Menu.background", pal.surface),
        ("*Menu.foreground", pal.fg),
        ("*Menu.activeBackground", pal.select_bg),
        ("*Menu.activeForeground", pal.select_fg),
    ):
        root.option_add(pattern, value)
    # An already-built drop-down keeps its colours unless told directly.
    try:
        for combo in _all(root):
            if combo.winfo_class() == "TCombobox":
                popdown = root.tk.call("ttk::combobox::PopdownWindow", combo._w)
                root.tk.call(f"{popdown}.f.l", "configure",
                             "-background", pal.field, "-foreground", pal.fg,
                             "-selectbackground", pal.select_bg,
                             "-selectforeground", pal.select_fg)
    except tk.TclError:
        pass


def _all(widget: tk.Misc):
    stack = [widget]
    while stack:
        w = stack.pop()
        yield w
        try:
            stack.extend(w.winfo_children())
        except tk.TclError:
            continue


def _recolor(w: tk.Misc, pal: Palette) -> None:
    if getattr(w, "theme_exempt", False):
        return
    try:
        cls = w.winfo_class()
        if cls in ("Tk", "Toplevel"):
            w.configure(background=pal.bg)
            title_bar(w, pal.dark)
        elif cls == "Frame":
            w.configure(background=pal.bg)
            if str(w.winfo_toplevel()) == str(w):  # a Detachable window
                title_bar(w, pal.dark)
        elif cls == "Listbox":
            w.configure(background=pal.field, foreground=pal.fg,
                        selectbackground=pal.select_bg, selectforeground=pal.select_fg,
                        highlightbackground=pal.border, highlightcolor=pal.accent,
                        disabledforeground=pal.muted)
        elif cls == "Text":
            w.configure(background=pal.field, foreground=pal.fg,
                        insertbackground=pal.fg, selectbackground=pal.select_bg,
                        selectforeground=pal.select_fg,
                        highlightbackground=pal.border, highlightcolor=pal.accent)
            if "chosen" in w.tag_names():
                w.tag_configure("chosen", background=pal.chosen_bg, foreground=pal.chosen_fg)
        elif cls == "Canvas":
            w.configure(background=pal.canvas, highlightbackground=pal.bg)
        elif cls == "Button":
            w.configure(background=pal.button, foreground=pal.fg,
                        activebackground=pal.button_active, activeforeground=pal.fg,
                        highlightbackground=pal.bg, disabledforeground=pal.muted)
        elif cls == "Label":
            w.configure(background=pal.tip_bg, foreground=pal.tip_fg)
        elif cls == "Menu":
            w.configure(background=pal.surface, foreground=pal.fg,
                        activebackground=pal.select_bg, activeforeground=pal.select_fg,
                        selectcolor=pal.fg)
    except tk.TclError:
        pass
