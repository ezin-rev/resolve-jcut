"""
Reusable compound widgets for the J-Cut plugin GUI.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

# Resolve marker colours available via API
MARKER_COLORS = ["Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream"]


class LabeledSlider(ttk.Frame):
    """Integer slider with a live numeric readout and direct-entry field."""

    def __init__(
        self,
        parent: tk.Widget,
        label: str,
        from_: int,
        to: int,
        default: int,
        unit: str = "frames",
        on_change: Optional[Callable[[int], None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, **kwargs)
        self._on_change = on_change
        self._var = tk.IntVar(value=default)

        ttk.Label(self, text=label, width=22, anchor="w").grid(row=0, column=0, sticky="w")

        self._slider = ttk.Scale(
            self,
            from_=from_,
            to=to,
            variable=self._var,
            orient="horizontal",
            command=self._slider_moved,
        )
        self._slider.grid(row=0, column=1, sticky="ew", padx=(4, 4))

        vcmd = (self.register(self._validate_int), "%P")
        self._entry = ttk.Entry(self, textvariable=self._var, width=5, validate="key", validatecommand=vcmd)
        self._entry.grid(row=0, column=2)
        self._entry.bind("<Return>", self._entry_committed)
        self._entry.bind("<FocusOut>", self._entry_committed)

        ttk.Label(self, text=unit).grid(row=0, column=3, padx=(2, 0))

        self.columnconfigure(1, weight=1)

    def _validate_int(self, value: str) -> bool:
        return value == "" or value.lstrip("-").isdigit()

    def _slider_moved(self, _event=None) -> None:
        if self._on_change:
            self._on_change(self._var.get())

    def _entry_committed(self, _event=None) -> None:
        if self._on_change:
            self._on_change(self._var.get())

    @property
    def value(self) -> int:
        return self._var.get()

    @value.setter
    def value(self, v: int) -> None:
        self._var.set(v)


class LogView(ttk.Frame):
    """Scrollable, read-only log output with colour-coded levels."""

    _TAGS = {
        "DEBUG": {"foreground": "#7f848e"},
        "INFO": {"foreground": "#d4d4d4"},
        "WARN": {"foreground": "#e5c07b"},
        "ERROR": {"foreground": "#e06c75"},
        "OK": {"foreground": "#98c379"},
        "HEAD": {"foreground": "#61afef", "font": ("Menlo", 10, "bold")},
    }

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self._text = tk.Text(
            self,
            state="disabled",
            bg="#1e2127",
            fg="#d4d4d4",
            font=("Menlo", 10),
            relief="flat",
            wrap="word",
        )
        scrollbar = ttk.Scrollbar(self, command=self._text.yview)
        self._text.configure(yscrollcommand=scrollbar.set)
        self._text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for tag, cfg in self._TAGS.items():
            self._text.tag_configure(tag, **cfg)

    def append(self, message: str, level: str = "INFO") -> None:
        tag = level if level in self._TAGS else "INFO"
        self._text.configure(state="normal")
        self._text.insert("end", message + "\n", tag)
        self._text.see("end")
        self._text.configure(state="disabled")

    def clear(self) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")


class StatusBar(ttk.Frame):
    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self._label = ttk.Label(self, text="Ready", anchor="w")
        self._label.pack(side="left", fill="x", expand=True, padx=4)
        self._progress = ttk.Progressbar(self, length=120, mode="indeterminate")
        self._progress.pack(side="right", padx=4)

    def set(self, text: str) -> None:
        self._label.config(text=text)

    def busy(self, running: bool) -> None:
        if running:
            self._progress.config(mode="indeterminate")
            self._progress.start(12)
        else:
            self._progress.stop()
            self._progress.config(mode="determinate", value=0)

    def set_progress(self, frac: float) -> None:
        self._progress.stop()
        self._progress.config(mode="determinate",
                              value=max(0.0, min(100.0, frac * 100.0)))
