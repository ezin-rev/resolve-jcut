# Testing & Debugging

## The offline suite

```bash
uv run python tests/test_autocut.py
```

No Resolve needed. It covers:

- pure cut/segment logic (`_merge_ranges`, `_keep_segments`, J-cut hold math)
- silence detection against synthetic audio
- the two-phase pipeline end-to-end against a **fake bridge responder** — a
  thread that answers the real file protocol, so `BridgeSession` is exercised
  unmodified
- FCPXML structure, including the J-cut audio leads
- `autocut_script.py`'s segment logic (the Studio script)

!!! warning "AUTOCUT_BRIDGE_DIR must be set before importing resolve_bridge"
    The test sets `AUTOCUT_BRIDGE_DIR` to an isolated directory *before* the
    `resolve_bridge` import, because the module resolves its bridge paths at
    import time. Break that ordering and a **real** bridge running inside
    Resolve answers the test's requests — tests mutate your open project and
    fail in ways that make no sense. Keep the ordering when adding tests.

## Compile checks for in-Resolve scripts

`bridge_script.py` and `autocut_script.py` run under Resolve's Python and
must stay stdlib-only and standalone:

```bash
uv run python -m py_compile autocut_script.py bridge_script.py
```

This catches accidental venv imports at review time instead of inside
Resolve's console.

## Detection CLI

Silence detection can be run against a single file, no GUI or Resolve:

```bash
.venv/bin/python cutlist.py path/to/clip.mp4 --fps 24
.venv/bin/python cutlist.py path/to/clip.mp4 --fps 24 --threshold-db -35
```

Prints the silence regions as JSON (`--threshold-db auto` is the default).
Use it to tune thresholds on problem footage — much faster than round-trips
through the GUI. This is also the subprocess entry point the Studio script
calls.

## Live-debugging Resolve

With the bridge running inside Resolve, its API can be probed from a
terminal — see
[Bridge Protocol → Probing Resolve](bridge-protocol.md#probing-resolve-from-a-terminal).

Other debugging notes:

- **Verbose logs:** `uv run python main.py --verbose` — bridge
  request/response tracing is at DEBUG level.
- **Stale bridge processes:** Resolve runs menu scripts as separate
  `fuscript` processes that can outlive it. The current bridge self-exits,
  but when in doubt: `ps -e | grep fuscript`.
- **Stale bridge code:** editing `bridge_script.py` does nothing until
  AutoCut Bridge is re-run inside Resolve.
- **macOS auto-launch:** LaunchAgent output lands in `~/.autocut/gui.log` —
  first place to look when one-click launch does nothing.
- **Print debugging inside Resolve:** the in-Resolve scripts use `print()`,
  which shows in Resolve's Console (Workspace → Console).

## Docs

```bash
uv run --group docs mkdocs serve    # live-preview at http://127.0.0.1:8000
```

The site deploys to GitHub Pages automatically on push to `main`
(`.github/workflows/docs.yml`, via `mkdocs gh-deploy`).
