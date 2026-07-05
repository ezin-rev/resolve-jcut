"""
Main application window — AutoCut/Firecut-style editor for DaVinci Resolve.

Two tabs:
  AutoCut       one-click pipeline: silence cut, filler words, punch-in zoom,
                captions (SRT), chapter markers → builds a NEW timeline
  J-Cut Markers analyses existing cuts and places J-cut/L-cut markers
"""

from __future__ import annotations

import logging
import queue
import threading

import tkinter as tk
from tkinter import messagebox, ttk

from config import JCutConfig
from gui.widgets import LabeledSlider, LogView, MARKER_COLORS, StatusBar
from transcribe import Cancelled

log = logging.getLogger("autocut.gui.app")

WHISPER_MODELS = ["thai-large-v3-mlx", "thai-distill-mlx", "base", "medium",
                  "large-v3-turbo-mlx", "thai-large-v3", "tiny", "small"]
LANGUAGES = ["auto", "en", "th", "ja", "zh", "ko", "de", "fr", "es", "pt"]

# Changing any of these after Analyze invalidates the previewed cuts
_DETECT_FIELDS = ("remove_silence", "auto_threshold", "silence_threshold_db",
                  "min_silence_sec", "padding_frames", "remove_fillers",
                  "filler_words", "model_size", "language")


class _TkLogHandler(logging.Handler):
    def __init__(self, post) -> None:
        super().__init__()
        self._post = post  # thread-safe post(fn, *args) — never touch Tk here

    def emit(self, record: logging.LogRecord) -> None:
        level_map = {
            logging.DEBUG: "DEBUG",
            logging.WARNING: "WARN",
            logging.ERROR: "ERROR",
            logging.CRITICAL: "ERROR",
        }
        tag = level_map.get(record.levelno, "INFO")
        msg = self.format(record)
        self._post(msg, tag)


class AutoCutApp(tk.Tk):
    def __init__(self, verbose: bool = False) -> None:
        super().__init__()
        self.title("AutoCut for DaVinci Resolve")
        self.minsize(600, 640)
        self._session = None
        self._connecting = False
        self._start_verbose = verbose
        self._running = False
        self._cancel_event: threading.Event | None = None
        self._analysis = None
        self._analysis_params = None
        self._analysis_timeline: str | None = None
        # Workers must never call Tk directly (silently broken on Tk 9) —
        # they post through this queue, drained on the main thread.
        self._ui_queue: queue.Queue = queue.Queue()
        self._build_ui()
        self._wire_logging()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._write_pid()
        self.after(50, self._drain_ui_queue)
        self.after(300, lambda: self._connect(auto=True))

    def _write_pid(self) -> None:
        # lets the bridge script detect a running app instead of opening twice
        import os
        try:
            from resolve_bridge import BRIDGE_DIR
            BRIDGE_DIR.mkdir(exist_ok=True)
            (BRIDGE_DIR / "gui.pid").write_text(str(os.getpid()))
        except OSError:
            pass

    def ui_call(self, fn, *args) -> None:
        """Thread-safe: run fn(*args) on the Tk main thread."""
        self._ui_queue.put((fn, args))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    log.exception("UI callback failed")
        except queue.Empty:
            pass
        self.after(50, self._drain_ui_queue)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # Connection row
        conn = ttk.Frame(root)
        conn.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        conn.columnconfigure(0, weight=1)
        self._conn_label = ttk.Label(conn, text="Not connected", foreground="#e06c75")
        self._conn_label.grid(row=0, column=0, sticky="w")
        ttk.Button(conn, text="Connect to Resolve", command=self._connect).grid(row=0, column=1)

        # Notebook
        self._nb = ttk.Notebook(root)
        self._nb.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self._build_autocut_tab(self._nb)
        self._build_preview_tab(self._nb)
        self._build_jcut_tab(self._nb)

        # Log
        log_frame = ttk.LabelFrame(root, text="Log", padding=4)
        log_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
        self._verbose_var = tk.BooleanVar(value=self._start_verbose)
        ttk.Checkbutton(log_frame, text="Verbose", variable=self._verbose_var,
                        command=self._toggle_verbose).pack(anchor="e")
        self._log_view = LogView(log_frame)
        self._log_view.pack(fill="both", expand=True)

        bottom = ttk.Frame(root)
        bottom.grid(row=3, column=0, sticky="ew")
        self._status = StatusBar(bottom)
        self._status.pack(side="left", fill="x", expand=True)
        self._cancel_btn = ttk.Button(bottom, text="Cancel",
                                      command=self._cancel_run, state="disabled")
        self._cancel_btn.pack(side="right", padx=(6, 0))

        self._last_plans = None

    def _build_autocut_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  AutoCut  ")
        tab.columnconfigure(0, weight=1)

        # Presets
        pre = ttk.Frame(tab)
        pre.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(pre, text="Preset:").pack(side="left")
        self._preset_var = tk.StringVar()
        self._preset_combo = ttk.Combobox(pre, textvariable=self._preset_var,
                                          state="readonly", width=18)
        self._preset_combo.pack(side="left", padx=(4, 6))
        self._preset_combo.bind("<<ComboboxSelected>>", self._load_preset)
        ttk.Button(pre, text="Save…", command=self._save_preset).pack(side="left")
        ttk.Button(pre, text="Delete", command=self._delete_preset).pack(
            side="left", padx=(4, 0))

        # Tracks
        tracks = ttk.Frame(tab)
        tracks.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(tracks, text="Video track:").pack(side="left")
        self._video_track_var = tk.IntVar(value=1)
        ttk.Spinbox(tracks, from_=1, to=20, textvariable=self._video_track_var,
                    width=4).pack(side="left", padx=(4, 16))
        ttk.Label(tracks, text="Audio track:").pack(side="left")
        self._audio_track_var = tk.IntVar(value=1)
        ttk.Spinbox(tracks, from_=1, to=20, textvariable=self._audio_track_var,
                    width=4).pack(side="left", padx=(4, 0))

        # Silence removal
        sil = ttk.LabelFrame(tab, text="Silence Removal", padding=6)
        sil.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self._remove_silence_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(sil, text="Remove silences",
                        variable=self._remove_silence_var).grid(
            row=0, column=0, columnspan=6, sticky="w")
        self._auto_threshold_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(sil, text="Auto threshold (measures each file)",
                        variable=self._auto_threshold_var).grid(
            row=0, column=6, sticky="w", padx=(12, 0))
        ttk.Label(sil, text="Threshold:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._threshold_var = tk.DoubleVar(value=-42.0)
        ttk.Spinbox(sil, from_=-80, to=-10, increment=1, format="%.0f",
                    textvariable=self._threshold_var, width=5).grid(
            row=1, column=1, padx=(4, 2), pady=(4, 0))
        ttk.Label(sil, text="dB   Min silence:").grid(row=1, column=2, pady=(4, 0))
        self._min_silence_var = tk.DoubleVar(value=0.40)
        ttk.Spinbox(sil, from_=0.1, to=5.0, increment=0.1, format="%.2f",
                    textvariable=self._min_silence_var, width=5).grid(
            row=1, column=3, padx=(4, 2), pady=(4, 0))
        ttk.Label(sil, text="sec   Padding:").grid(row=1, column=4, pady=(4, 0))
        self._padding_var = tk.IntVar(value=6)
        ttk.Spinbox(sil, from_=0, to=48, textvariable=self._padding_var,
                    width=4).grid(row=1, column=5, padx=(4, 0), pady=(4, 0))

        # AI features (Whisper)
        ai = ttk.LabelFrame(tab, text="AI Features (Whisper — offline, free)", padding=6)
        ai.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        ai.columnconfigure(1, weight=1)

        self._fillers_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ai, text="Remove filler words:",
                        variable=self._fillers_var).grid(row=0, column=0, sticky="w")
        self._filler_words_var = tk.StringVar(value="um, uh, uhm, uhh, er, erm, hmm")
        ttk.Entry(ai, textvariable=self._filler_words_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=(4, 0))

        self._captions_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ai, text="Generate captions (SRT file, auto-imported)",
                        variable=self._captions_var).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 0))

        self._chapters_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ai, text="Chapter markers on pauses longer than",
                        variable=self._chapters_var).grid(row=2, column=0, sticky="w", pady=(2, 0))
        self._chapter_gap_var = tk.DoubleVar(value=2.0)
        ttk.Spinbox(ai, from_=0.5, to=10.0, increment=0.5, format="%.1f",
                    textvariable=self._chapter_gap_var, width=5).grid(
            row=2, column=1, sticky="w", padx=(4, 2), pady=(2, 0))
        ttk.Label(ai, text="sec").grid(row=2, column=2, sticky="w", pady=(2, 0))

        lang_row = ttk.Frame(ai)
        lang_row.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(lang_row, text="Language:").pack(side="left")
        self._language_var = tk.StringVar(value="auto")
        ttk.Combobox(lang_row, textvariable=self._language_var, values=LANGUAGES,
                     width=6, state="readonly").pack(side="left", padx=(4, 16))
        ttk.Label(lang_row, text="Model:").pack(side="left")
        self._model_var = tk.StringVar(value="base")
        # editable: any HuggingFace CTranslate2 repo id works too
        ttk.Combobox(lang_row, textvariable=self._model_var, values=WHISPER_MODELS,
                     width=14).pack(side="left", padx=(4, 0))

        # Style
        style = ttk.LabelFrame(tab, text="Style", padding=6)
        style.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        self._punch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(style, text="Auto punch-in (alternate zoom on cuts)",
                        variable=self._punch_var).pack(side="left")
        self._zoom_var = tk.DoubleVar(value=1.15)
        ttk.Spinbox(style, from_=1.05, to=1.5, increment=0.05, format="%.2f",
                    textvariable=self._zoom_var, width=5).pack(side="left", padx=(8, 2))
        ttk.Label(style, text="×").pack(side="left")

        # Build mode
        mode = ttk.LabelFrame(tab, text="Build Mode", padding=6)
        mode.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        self._build_mode_var = tk.StringVar(value="stream")
        ttk.Radiobutton(
            mode, text="Live append — watch cuts land in Resolve clip by clip",
            variable=self._build_mode_var, value="stream",
        ).pack(anchor="w")
        ttk.Radiobutton(
            mode, text="FCPXML export — for Premiere/FCP interchange "
                       "(Resolve free imports it with OFFLINE media)",
            variable=self._build_mode_var, value="fcpxml",
        ).pack(anchor="w")
        jrow = ttk.Frame(mode)
        jrow.pack(anchor="w", pady=(4, 0))
        ttk.Label(jrow, text="J-cut audio lead:").pack(side="left")
        self._jcut_lead_var = tk.IntVar(value=0)
        ttk.Spinbox(jrow, from_=0, to=48, textvariable=self._jcut_lead_var,
                    width=4).pack(side="left", padx=(4, 2))
        ttk.Label(jrow, text="frames — previous shot holds while the next "
                             "line starts (voice is never moved)",
                  foreground="#888888").pack(side="left")

        # Run
        run_row = ttk.Frame(tab)
        run_row.grid(row=5, column=0, sticky="ew")
        self._autocut_btn = ttk.Button(
            run_row, text="▶  Run AutoCut", command=self._run_autocut, state="disabled"
        )
        self._autocut_btn.pack(side="left")
        self._preview_btn = ttk.Button(
            run_row, text="Analyze (preview)", command=self._analyze_preview,
            state="disabled",
        )
        self._preview_btn.pack(side="left", padx=(8, 0))
        ttk.Label(
            run_row,
            text="Builds a NEW timeline — your original is never modified.",
            foreground="#888888",
        ).pack(side="left", padx=(12, 0))

        self._refresh_preset_list()

    def _build_preview_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  Preview  ")
        self._preview_tab = tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        cols = ("use", "clip", "start", "end", "length", "reason")
        tv = ttk.Treeview(tab, columns=cols, show="headings", height=11)
        for cid, width, text, anchor in [
            ("use", 40, "Use", "center"), ("clip", 140, "Clip", "w"),
            ("start", 70, "Start", "e"), ("end", 70, "End", "e"),
            ("length", 70, "Length", "e"), ("reason", 180, "Reason", "w"),
        ]:
            tv.heading(cid, text=text)
            tv.column(cid, width=width, anchor=anchor, stretch=(cid in ("clip", "reason")))
        tv.tag_configure("off", foreground="#7f848e")
        tv.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tab, command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        tv.bind("<Button-1>", self._on_preview_click)
        tv.bind("<Double-1>", self._on_preview_double)
        self._preview_tree = tv

        self._preview_summary = ttk.Label(
            tab, text="No analysis yet — use Analyze (preview) on the AutoCut tab.")
        self._preview_summary.grid(row=1, column=0, columnspan=2,
                                   sticky="w", pady=(6, 6))

        btns = ttk.Frame(tab)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._build_btn = ttk.Button(btns, text="▶  Build Timeline",
                                     command=self._build_timeline, state="disabled")
        self._build_btn.pack(side="left")
        ttk.Label(btns, text="Click a row's ✓ to keep that section instead "
                             "of cutting it.",
                  foreground="#888888").pack(side="left", padx=(12, 0))

    def _build_jcut_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="  J-Cut Markers  ")
        tab.columnconfigure(0, weight=1)

        self._jcut_slider = LabeledSlider(tab, "J-cut duration", 0, 120, 24, unit="frames")
        self._jcut_slider.grid(row=0, column=0, sticky="ew", pady=2)
        self._lcut_slider = LabeledSlider(tab, "L-cut duration", 0, 120, 0, unit="frames")
        self._lcut_slider.grid(row=1, column=0, sticky="ew", pady=2)
        self._min_dur_slider = LabeledSlider(tab, "Min clip length", 1, 120, 12, unit="frames")
        self._min_dur_slider.grid(row=2, column=0, sticky="ew", pady=2)

        opts = ttk.Frame(tab)
        opts.grid(row=3, column=0, sticky="ew", pady=(6, 6))
        ttk.Label(opts, text="Marker colour:").pack(side="left")
        self._marker_color_var = tk.StringVar(value="Blue")
        ttk.Combobox(opts, textvariable=self._marker_color_var, values=MARKER_COLORS,
                     width=10, state="readonly").pack(side="left", padx=(4, 0))

        btns = ttk.Frame(tab)
        btns.grid(row=4, column=0, sticky="ew")
        self._analyse_btn = ttk.Button(btns, text="Analyse Timeline",
                                       command=self._analyse, state="disabled")
        self._analyse_btn.pack(side="left", padx=(0, 8))
        self._apply_btn = ttk.Button(btns, text="Place J-Cut Markers",
                                     command=self._apply, state="disabled")
        self._apply_btn.pack(side="left")
        ttk.Button(btns, text="Clear Log", command=self._clear_log).pack(side="right")

    # ── Logging ───────────────────────────────────────────────────────────────

    def _toggle_verbose(self) -> None:
        verbose = self._verbose_var.get()
        logging.getLogger("autocut").setLevel(
            logging.DEBUG if verbose else logging.INFO
        )
        log.info("Verbose logging %s", "on" if verbose else "off")

    def _wire_logging(self) -> None:
        handler = _TkLogHandler(
            lambda msg, tag: self.ui_call(self._log_view.append, msg, tag)
        )
        handler.setFormatter(logging.Formatter("%(levelname)-5s  %(message)s"))
        # Scope to 'autocut.*' only — avoids numba/librosa flooding the event queue
        app_log = logging.getLogger("autocut")
        app_log.addHandler(handler)
        app_log.setLevel(logging.DEBUG if self._verbose_var.get() else logging.INFO)
        app_log.propagate = False

    # ── Connect ───────────────────────────────────────────────────────────────

    def _connect(self, auto: bool = False) -> None:
        if self._connecting or (auto and self._session is not None):
            return
        self._connecting = True
        self._status.set("Connecting…")
        self._status.busy(True)
        worker = threading.Thread(target=self._connect_worker, args=(auto,), daemon=True)
        worker.start()
        # A hung attempt must never brick the connect loop — abandon and retry
        self.after(20000, self._connect_watchdog, worker)

    def _connect_watchdog(self, worker: threading.Thread) -> None:
        if not worker.is_alive() or self._session is not None:
            return
        log.warning("Connect attempt timed out after 20s — retrying")
        self._connecting = False
        self._status.busy(False)
        self._connect(auto=True)

    def _connect_worker(self, auto: bool) -> None:
        try:
            from resolve_bridge import connect
            self._session = connect()
            fps = self._session.fps()
            name = self._session.timeline_name()
            mode = type(self._session).__name__
            self.ui_call(self._on_connected, name, fps, mode)
        except Exception as exc:
            self.ui_call(self._on_connect_failed, str(exc), auto)

    def _on_connected(self, timeline_name: str, fps: float, mode: str) -> None:
        self._connecting = False
        self._status.busy(False)
        via = "bridge (free version)" if mode == "BridgeSession" else "direct API (Studio)"
        self._conn_label.config(
            text=f"Connected via {via} — {timeline_name}  ({fps:.2f} fps)",
            foreground="#98c379",
        )
        self._status.set("Connected.")
        self._set_running(False)
        log.info("Connected via %s: %s (%.2f fps)", via, timeline_name, fps)

    def _on_connect_failed(self, error: str, auto: bool = False) -> None:
        self._connecting = False
        self._status.busy(False)
        if auto:
            # Keep retrying quietly until the bridge script is started in Resolve
            self._conn_label.config(
                text="Waiting for Resolve — run Workspace → Scripts → Utility → AutoCut Bridge",
                foreground="#e5c07b",
            )
            self._status.set("Waiting for AutoCut Bridge…")
            self.after(3000, lambda: self._connect(auto=True))
            return
        self._conn_label.config(text="Connection failed", foreground="#e06c75")
        self._status.set("Error.")
        log.error("Connect failed: %s", error)
        messagebox.showerror("Connection Error", error, parent=self)

    # ── Run state ─────────────────────────────────────────────────────────────

    def _set_running(self, running: bool) -> None:
        self._running = running
        connected = self._session is not None
        state = "disabled" if (running or not connected) else "normal"
        for btn in (self._autocut_btn, self._preview_btn,
                    self._analyse_btn, self._apply_btn):
            btn.config(state=state)
        self._build_btn.config(
            state="normal" if (not running and connected and self._analysis)
            else "disabled")
        self._cancel_btn.config(
            state="normal" if (running and self._cancel_event) else "disabled")
        self._status.busy(running)

    def _begin_run(self) -> threading.Event:
        self._cancel_event = threading.Event()
        self._set_running(True)
        return self._cancel_event

    def _end_run(self) -> None:
        self._cancel_event = None
        self._set_running(False)

    def _cancel_run(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
            self._cancel_btn.config(state="disabled")
            self._status.set("Cancelling…")

    def _on_cancelled(self) -> None:
        self._end_run()
        self._log_view.append(
            "Cancelled. A partially built timeline may remain in Resolve.", "WARN")
        self._status.set("Cancelled.")

    def _on_progress(self, frac: float, msg: str) -> None:
        self._status.set_progress(frac)
        if msg:
            self._status.set(msg)

    # ── Params ────────────────────────────────────────────────────────────────

    def _collect_params(self):
        from autocut import AutoCutParams
        return AutoCutParams(
            remove_silence=self._remove_silence_var.get(),
            auto_threshold=self._auto_threshold_var.get(),
            silence_threshold_db=self._threshold_var.get(),
            min_silence_sec=self._min_silence_var.get(),
            padding_frames=self._padding_var.get(),
            remove_fillers=self._fillers_var.get(),
            filler_words=self._filler_words_var.get(),
            punch_in=self._punch_var.get(),
            zoom_amount=self._zoom_var.get(),
            captions=self._captions_var.get(),
            chapters=self._chapters_var.get(),
            chapter_gap_sec=self._chapter_gap_var.get(),
            model_size=self._model_var.get(),
            language="" if self._language_var.get() == "auto" else self._language_var.get(),
            build_mode=self._build_mode_var.get(),
            jcut_frames=self._jcut_lead_var.get(),
        )

    def _autocut_config(self) -> JCutConfig:
        return JCutConfig(
            video_track=self._video_track_var.get(),
            audio_track=self._audio_track_var.get(),
            silence_threshold_db=self._threshold_var.get(),
        )

    # ── Presets ───────────────────────────────────────────────────────────────

    def _param_vars(self) -> dict:
        return {
            "remove_silence": self._remove_silence_var,
            "auto_threshold": self._auto_threshold_var,
            "silence_threshold_db": self._threshold_var,
            "min_silence_sec": self._min_silence_var,
            "padding_frames": self._padding_var,
            "remove_fillers": self._fillers_var,
            "filler_words": self._filler_words_var,
            "punch_in": self._punch_var,
            "zoom_amount": self._zoom_var,
            "captions": self._captions_var,
            "chapters": self._chapters_var,
            "chapter_gap_sec": self._chapter_gap_var,
            "model_size": self._model_var,
            "build_mode": self._build_mode_var,
            "jcut_frames": self._jcut_lead_var,
            "video_track": self._video_track_var,
            "audio_track": self._audio_track_var,
        }

    def _refresh_preset_list(self, select: str = "") -> None:
        from presets import load_presets
        self._preset_combo["values"] = sorted(load_presets())
        if select:
            self._preset_var.set(select)

    def _save_preset(self) -> None:
        from tkinter import simpledialog
        name = simpledialog.askstring(
            "Save Preset", "Preset name:",
            initialvalue=self._preset_var.get(), parent=self)
        if not name:
            return
        from presets import save_preset
        values = {k: v.get() for k, v in self._param_vars().items()}
        values["language"] = self._language_var.get()
        save_preset(name, values)
        self._refresh_preset_list(select=name)
        self._log_view.append(f"Preset '{name}' saved.", "OK")

    def _delete_preset(self) -> None:
        name = self._preset_var.get()
        if not name:
            return
        from presets import delete_preset
        delete_preset(name)
        self._preset_var.set("")
        self._refresh_preset_list()
        self._log_view.append(f"Preset '{name}' deleted.", "INFO")

    def _load_preset(self, _event=None) -> None:
        from presets import load_presets
        values = load_presets().get(self._preset_var.get())
        if not values:
            return
        for key, var in self._param_vars().items():
            if key in values:
                var.set(values[key])
        self._language_var.set(values.get("language") or "auto")
        self._log_view.append(f"Preset '{self._preset_var.get()}' loaded.", "INFO")

    # ── AutoCut ───────────────────────────────────────────────────────────────

    def _run_autocut(self) -> None:
        if self._session is None:
            messagebox.showwarning("Not connected", "Connect to Resolve first.", parent=self)
            return
        params = self._collect_params()
        config = self._autocut_config()
        self._log_view.append("─── Running AutoCut ───", "HEAD")
        if params.needs_transcript():
            self._log_view.append(
                "Transcribing with Whisper — first run downloads the model, be patient…",
                "INFO",
            )
        self._status.set("Running AutoCut…")
        cancel = self._begin_run()
        threading.Thread(target=self._autocut_worker,
                         args=(config, params, cancel), daemon=True).start()

    def _autocut_worker(self, config: JCutConfig, params, cancel) -> None:
        try:
            from autocut import run_autocut
            self._session.refresh_timeline()
            config.fps = self._session.fps()
            result = run_autocut(
                self._session, config, params,
                progress=lambda f, m: self.ui_call(self._on_progress, f, m),
                cancel=cancel,
            )
            self.ui_call(self._on_autocut_done, result)
        except Cancelled:
            self.ui_call(self._on_cancelled)
        except Exception as exc:
            log.exception("AutoCut failed")
            self.ui_call(self._on_run_error, str(exc))

    # ── Analyze → Preview ─────────────────────────────────────────────────────

    def _analyze_preview(self) -> None:
        if self._session is None:
            messagebox.showwarning("Not connected", "Connect to Resolve first.", parent=self)
            return
        params = self._collect_params()
        config = self._autocut_config()
        self._log_view.append("─── Analyzing (preview) ───", "HEAD")
        if params.needs_transcript():
            self._log_view.append(
                "Transcribing with Whisper — first run downloads the model, be patient…",
                "INFO",
            )
        self._status.set("Analyzing…")
        cancel = self._begin_run()
        threading.Thread(target=self._analyze_worker,
                         args=(config, params, cancel), daemon=True).start()

    def _analyze_worker(self, config: JCutConfig, params, cancel) -> None:
        try:
            from autocut import analyze_autocut
            self._session.refresh_timeline()
            config.fps = self._session.fps()
            timeline = self._session.timeline_name()
            analysis = analyze_autocut(
                self._session, config, params,
                progress=lambda f, m: self.ui_call(self._on_progress, f, m),
                cancel=cancel,
            )
            self.ui_call(self._on_analysis_done, analysis, params, timeline)
        except Cancelled:
            self.ui_call(self._on_cancelled)
        except Exception as exc:
            log.exception("Analyze failed")
            self.ui_call(self._on_run_error, str(exc))

    def _on_analysis_done(self, analysis, params, timeline_name: str) -> None:
        self._analysis = analysis
        self._analysis_params = params
        self._analysis_timeline = timeline_name
        self._end_run()
        self._populate_preview()
        self._nb.select(self._preview_tab)
        self._status.set(f"Analysis complete — {len(analysis.cuts)} cuts proposed.")

    def _populate_preview(self) -> None:
        tv = self._preview_tree
        tv.delete(*tv.get_children())
        fps = self._analysis.fps
        for i, cut in enumerate(self._analysis.cuts):
            tv.insert("", "end", iid=str(i), values=(
                "✓" if cut.enabled else "✗",
                cut.clip_name,
                f"{cut.start_frame / fps:.2f}s",
                f"{cut.end_frame / fps:.2f}s",
                f"{cut.duration_sec(fps):.2f}s",
                cut.reason,
            ), tags=() if cut.enabled else ("off",))
        self._update_preview_summary()

    def _on_preview_click(self, event) -> None:
        tv = self._preview_tree
        if tv.identify_region(event.x, event.y) != "cell":
            return
        if tv.identify_column(event.x) != "#1":
            return
        row = tv.identify_row(event.y)
        if row:
            self._toggle_cut(int(row))

    def _on_preview_double(self, event) -> None:
        row = self._preview_tree.identify_row(event.y)
        if row:
            self._toggle_cut(int(row))

    def _toggle_cut(self, index: int) -> None:
        if self._running or self._analysis is None:
            return
        cut = self._analysis.cuts[index]
        cut.enabled = not cut.enabled
        iid = str(index)
        self._preview_tree.set(iid, "use", "✓" if cut.enabled else "✗")
        self._preview_tree.item(iid, tags=() if cut.enabled else ("off",))
        self._update_preview_summary()

    def _update_preview_summary(self) -> None:
        from autocut import build_segments
        pipe_log = logging.getLogger("autocut.pipeline")
        pipe_log.disabled = True   # toggling rows must not spam the log
        try:
            segments = build_segments(self._analysis, self._analysis_params)
        finally:
            pipe_log.disabled = False
        fps = self._analysis.fps
        total = self._analysis.total_frames()
        kept = sum(s.src_out - s.src_in for s in segments)
        enabled = sum(1 for c in self._analysis.cuts if c.enabled)
        self._preview_summary.config(text=(
            f"{enabled}/{len(self._analysis.cuts)} cuts enabled — removing "
            f"{max(total - kept, 0) / fps:.1f}s of {total / fps:.1f}s — "
            f"{len(segments)} segments"))

    # ── Build from preview ────────────────────────────────────────────────────

    def _build_timeline(self) -> None:
        if self._session is None or self._analysis is None:
            return
        params = self._collect_params()
        if any(getattr(params, f) != getattr(self._analysis_params, f)
               for f in _DETECT_FIELDS):
            self._log_view.append(
                "Detection settings changed since Analyze — the previewed cuts "
                "still reflect the old settings. Re-run Analyze to refresh.", "WARN")
        if params.needs_transcript() and not self._analysis.transcripts:
            self._log_view.append(
                "No transcript in this analysis — captions will be skipped and "
                "chapter titles will be generic. Re-run Analyze to include them.",
                "WARN")
        self._log_view.append("─── Building timeline from preview ───", "HEAD")
        self._status.set("Building timeline…")
        cancel = self._begin_run()
        threading.Thread(
            target=self._apply_worker,
            args=(self._autocut_config(), params, self._analysis, cancel),
            daemon=True).start()

    def _apply_worker(self, config: JCutConfig, params, analysis, cancel) -> None:
        try:
            from autocut import apply_autocut
            self._session.refresh_timeline()
            current = self._session.timeline_name()
            if self._analysis_timeline and current != self._analysis_timeline:
                raise RuntimeError(
                    f"Timeline changed since Analyze ('{current}' is now open, "
                    f"analysis was of '{self._analysis_timeline}') — switch back "
                    "in Resolve or re-run Analyze.")
            config.fps = self._session.fps()
            result = apply_autocut(
                self._session, config, params, analysis,
                progress=lambda f, m: self.ui_call(self._on_progress, f, m),
                cancel=cancel,
            )
            self.ui_call(self._on_autocut_done, result)
        except Cancelled:
            self.ui_call(self._on_cancelled)
        except Exception as exc:
            log.exception("Build failed")
            self.ui_call(self._on_run_error, str(exc))

    def _on_autocut_done(self, result: dict) -> None:
        self._end_run()
        self._log_view.append(f"New timeline: {result['timeline']}", "OK")
        self._log_view.append(
            f"  {result['segments']} segments, "
            f"~{result['removed_sec']:.1f}s removed", "OK")
        if "zoomed_clips" in result:
            self._log_view.append(f"  Punch-in applied to {result['zoomed_clips']} clips", "OK")
        if "fcpxml_path" in result:
            self._log_view.append(f"  FCPXML saved: {result['fcpxml_path']}", "OK")
        if "srt_path" in result:
            if result.get("srt_on_timeline"):
                self._log_view.append(
                    f"  {result['captions']} captions placed on the subtitle "
                    f"track ({result['srt_path']})", "OK")
            else:
                self._log_view.append(
                    f"  {result['captions']} captions → {result['srt_path']} "
                    "(in the media pool — drag onto a subtitle track)", "WARN")
        if "chapters" in result:
            self._log_view.append(f"  {result['chapters']} chapter markers placed", "OK")
        if "jcut_frames" in result:
            self._log_view.append(
                f"  J-cut audio lead: {result['jcut_frames']} frames", "OK")
        self._status.set("AutoCut complete.")

    # ── J-Cut markers ─────────────────────────────────────────────────────────

    def _jcut_config(self, dry_run: bool) -> JCutConfig:
        return JCutConfig(
            jcut_frames=self._jcut_slider.value,
            lcut_frames=self._lcut_slider.value,
            video_track=self._video_track_var.get(),
            audio_track=self._audio_track_var.get(),
            min_clip_duration_frames=self._min_dur_slider.value,
            marker_color=self._marker_color_var.get(),
            dry_run=dry_run,
        )

    def _analyse(self) -> None:
        if self._session is None:
            messagebox.showwarning("Not connected", "Connect to Resolve first.", parent=self)
            return
        self._log_view.append("─── Analysing timeline ───", "HEAD")
        self._status.set("Analysing…")
        self._cancel_event = None       # J-cut runs are quick, no cancel
        self._set_running(True)
        threading.Thread(target=self._jcut_worker,
                         args=(self._jcut_config(True), False), daemon=True).start()

    def _apply(self) -> None:
        if self._session is None:
            messagebox.showwarning("Not connected", "Connect to Resolve first.", parent=self)
            return
        self._log_view.append("─── Placing J-cut markers ───", "HEAD")
        self._status.set("Placing markers…")
        self._cancel_event = None
        self._set_running(True)
        threading.Thread(target=self._jcut_worker,
                         args=(self._jcut_config(True), True), daemon=True).start()

    def _jcut_worker(self, config: JCutConfig, apply: bool) -> None:
        try:
            from jcut_engine import build_plan, apply_jcuts
            from timeline_reader import read_timeline

            self._session.refresh_timeline()
            snapshot = read_timeline(self._session, config.video_track, config.audio_track)
            config.fps = snapshot.fps
            plans = build_plan(snapshot, config)
            if apply:
                results = apply_jcuts(plans, self._session, config)
            else:
                results = {
                    "total": len(plans),
                    "feasible": sum(1 for p in plans if p.feasible),
                    "applied": 0, "marked": 0,
                    "skipped": sum(1 for p in plans if not p.feasible),
                    "errors": [],
                }
            results["plans"] = plans
            self.ui_call(self._on_jcut_done, results, apply)
        except Exception as exc:
            log.exception("J-cut run failed")
            self.ui_call(self._on_run_error, str(exc))

    def _on_jcut_done(self, results: dict, applied: bool) -> None:
        self._end_run()
        for plan in results.get("plans", []):
            self._log_view.append(f"  {plan.describe()}",
                                  "OK" if plan.feasible else "WARN")
        for err in results["errors"]:
            self._log_view.append(f"  ERROR: {err}", "ERROR")
        summary = (f"Cuts: {results['total']}  |  Feasible: {results['feasible']}"
                   f"  |  Skipped: {results['skipped']}")
        if applied:
            summary += f"  |  Markers placed: {results['marked']}"
        self._log_view.append(summary, "HEAD")
        self._status.set(summary)

    # ── Common ────────────────────────────────────────────────────────────────

    def _on_run_error(self, error: str) -> None:
        self._end_run()
        self._status.set("Error.")
        messagebox.showerror("Error", error, parent=self)

    def _clear_log(self) -> None:
        self._log_view.clear()

    def _on_close(self) -> None:
        self._session = None
        try:
            from resolve_bridge import BRIDGE_DIR
            (BRIDGE_DIR / "gui.pid").unlink(missing_ok=True)
        except OSError:
            pass
        self.destroy()
