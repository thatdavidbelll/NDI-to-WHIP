# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Single-file Python application (`ndi_to_whip.py`) that bridges a named NDI source to a WHIP WebRTC ingest endpoint using a GStreamer pipeline. The pipeline is built as a string and launched via `Gst.parse_launch()`. Both the NDI plugin (`libgstndi.so`) and the WHIP client plugin (`libgstrswebrtc.so`) are compiled from [`gst-plugins-rs`](https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs) — they are **not** the same-named files in Ubuntu's `gstreamer1.0-plugins-bad` package.

## Running the application

```bash
# Activate venv first (installed to /opt/ndi_to_whip/venv on the server)
source /opt/ndi_to_whip/venv/bin/activate

# Required environment (set in the systemd unit; needed for manual runs)
export GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0
export LD_LIBRARY_PATH=/usr/local/lib

# Discover NDI sources on the network
python ndi_to_whip.py --probe

# Print the generated GStreamer pipeline string without running it
python ndi_to_whip.py --config /etc/ndi_to_whip/config.toml --print-pipeline

# Validate all GStreamer elements are present
python ndi_to_whip.py --validate

# Run with config file
python ndi_to_whip.py --config /etc/ndi_to_whip/config.toml

# Run with inline overrides (no config file needed)
python ndi_to_whip.py --ndi-source "PC (Stream 1)" --whip-url "http://localhost:8889/live/whip"
```

## Service management

```bash
sudo systemctl enable --now ndi-to-whip
sudo systemctl restart ndi-to-whip          # required after config.toml changes
sudo journalctl -u ndi-to-whip -f
sudo bash validate.sh                        # pre-flight checklist
```

## Architecture

All logic lives in `ndi_to_whip.py`. There are no other Python modules.

**`Config` dataclass** — all tunable parameters with defaults. `load_config()` merges a TOML file first, then CLI `--overrides` win. TOML sections map to config fields: `[ndi]`, `[whip]`, `[video]`, `[audio]`, `[retry]`, `[logging]`.

**`ENCODER_PROFILES` dict** — maps encoder name (`x264`, `vaapi`, `nvenc`) to a tuple of the GStreamer encoder element string, caps filter string, and RTP payloader string. Adding a new encoder means adding an entry here and nowhere else.

**`build_pipeline_string(cfg)`** — assembles the full GStreamer `parse_launch` string. The pipeline has two branches from `ndisrcdemux` (video and audio) that both terminate at the same named `whipclientsink` element. If `ndisrcdemux` pad names differ in a given plugin version, this is the only function to change.

**`NdiToWhipBridge`** — manages the GStreamer lifecycle:
- `_create_pipeline()` calls `Gst.parse_launch()` and raises with a human-readable hint on failure
- `_on_bus_message()` handles `ERROR`/`EOS` (triggers reconnect), `STATE_CHANGED` (logged), and `ELEMENT` messages (`whip-connected` / `whip-error` / `whip-disconnected` emitted by `whipclientsink`)
- `_run_once()` runs the pipeline inside a GLib main loop on a daemon thread; returns when the loop exits
- `run()` is the outer retry loop with exponential back-off; `_stop_event` (a `threading.Event`) makes the sleep between attempts interruptible by `stop()`

**`install.sh`** — idempotent installer. The version of `gst-plugins-rs` to build is pinned in the `GST_PLUGINS_RS_REV` variable at the top of the file. The NDI SDK must be installed manually before running this script (proprietary EULA).

**`validate.sh`** — checks system packages, NDI SDK paths, GStreamer element availability, Python venv, config placeholders, network/firewall, and service state. Exit code 0 = all pass, 1 = any failure.

## Key constraints

- `GST_PLUGIN_PATH` must point to `/usr/local/lib/gstreamer-1.0` so the `gst-plugins-rs` builds of `libgstndi.so` and `libgstrswebrtc.so` load instead of the Ubuntu system versions.
- The `ndisrcdemux` pad names (`demux.video` / `demux.audio`) vary between plugin builds. If the pipeline fails to link, run `gst-inspect-1.0 ndisrcdemux` on the target system to find the actual pad template names.
- `whipclientsink` and `webrtcbin` are different elements from different builds of `libgstrswebrtc.so`. Only the `gst-plugins-rs` build provides `whipclientsink`.
- Python's `tomllib` is stdlib in 3.11+. On 3.10 and earlier the venv must have `tomli` installed.
