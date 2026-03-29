#!/usr/bin/env python3
"""
ndi_to_whip.py — Production NDI → WHIP bridge

Captures a named NDI source via GStreamer and publishes it to a
WHIP (WebRTC HTTP Ingest Protocol) endpoint with structured logging,
retry/reconnect logic, and graceful shutdown.

Requirements:
  - GStreamer 1.20+
  - gst-plugin-ndi   (libgstndi.so   from gst-plugins-rs)
  - gst-plugin-webrtc (libgstrswebrtc.so from gst-plugins-rs, provides whipclientsink)
  - PyGObject (gi), tomli/tomllib

Usage:
  python ndi_to_whip.py --config /etc/ndi_to_whip/config.toml
  python ndi_to_whip.py --probe               # list visible NDI sources
  python ndi_to_whip.py --print-pipeline      # print GStreamer pipeline string
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Python 3.11+ has tomllib in stdlib; older Pythons need tomli ─────────────
try:
    import tomllib                  # type: ignore[import]
except ImportError:
    try:
        import tomli as tomllib     # type: ignore[import]
    except ImportError:
        tomllib = None              # type: ignore[assignment]

# ── GStreamer ─────────────────────────────────────────────────────────────────
try:
    import gi
    gi.require_version("Gst",    "1.0")
    gi.require_version("GLib",   "2.0")
    gi.require_version("GstWebRTC", "1.0")
    from gi.repository import Gst, GLib, GstWebRTC   # noqa: F401
except Exception as exc:
    sys.exit(f"[FATAL] GStreamer Python bindings not available: {exc}")

Gst.init(None)

# ── Structured logging ────────────────────────────────────────────────────────
try:
    import structlog
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
    )
    log = structlog.get_logger("ndi_to_whip")
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    log = logging.getLogger("ndi_to_whip")  # type: ignore[assignment]


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class Config:
    # NDI source
    ndi_source_name: str          = "MYCOMPUTER (Stream 1)"
    ndi_connect_timeout_ms: int   = 15000   # ms to wait for NDI source on startup

    # WHIP endpoint
    whip_url: str                 = "https://your-whip-endpoint/whip"
    auth_token: str               = ""      # Bearer token (empty = no auth header)
    stun_server: str              = "stun://stun.l.google.com:19302"
    turn_server: str              = ""      # e.g. "turn://user:pass@turn.example.com"

    # Video
    video_width: int              = 1920
    video_height: int             = 1080
    video_framerate: int          = 30
    video_bitrate_kbps: int       = 4000    # kbps for H.264 encoder
    video_encoder: str            = "x264"  # x264 | vaapi | nvenc
    video_keyframe_interval: int  = 0       # 0 = 2× framerate

    # Audio
    audio_channels: int           = 2
    audio_sample_rate: int        = 48000   # Hz
    audio_bitrate_bps: int        = 128000  # bps for Opus encoder

    # Retry / reconnect
    retry_max_attempts: int       = 0       # 0 = unlimited
    retry_initial_delay_s: float  = 2.0
    retry_max_delay_s: float      = 60.0
    retry_backoff_factor: float   = 2.0

    # Logging
    log_level: str                = "INFO"
    log_gst_debug: str            = "2"     # GST_DEBUG level (0–9)

    @property
    def keyframe_interval(self) -> int:
        return self.video_keyframe_interval or (self.video_framerate * 2)


def load_config(config_path: Optional[str], overrides: dict) -> Config:
    """
    Load config from a TOML file (optional) then apply CLI overrides.
    TOML is grouped under [ndi], [whip], [video], [audio], [retry], [logging].
    """
    cfg = Config()

    if config_path:
        path = Path(config_path)
        if not path.exists():
            log.warning("config_file_not_found", path=str(path))
        else:
            if tomllib is None:
                log.warning(
                    "toml_not_available",
                    hint="pip install tomli  (Python < 3.11)",
                )
            else:
                mode = "rb" if hasattr(tomllib, "load") else "rb"
                with open(path, "rb") as fh:
                    data = tomllib.load(fh)

                def _apply(section: str, mapping: dict[str, str]) -> None:
                    for toml_key, attr in mapping.items():
                        val = data.get(section, {}).get(toml_key)
                        if val is not None:
                            setattr(cfg, attr, val)

                _apply("ndi",    {
                    "source_name":        "ndi_source_name",
                    "connect_timeout_ms": "ndi_connect_timeout_ms",
                })
                _apply("whip",   {
                    "url":          "whip_url",
                    "auth_token":   "auth_token",
                    "stun_server":  "stun_server",
                    "turn_server":  "turn_server",
                })
                _apply("video",  {
                    "width":              "video_width",
                    "height":             "video_height",
                    "framerate":          "video_framerate",
                    "bitrate_kbps":       "video_bitrate_kbps",
                    "encoder":            "video_encoder",
                    "keyframe_interval":  "video_keyframe_interval",
                })
                _apply("audio",  {
                    "channels":     "audio_channels",
                    "sample_rate":  "audio_sample_rate",
                    "bitrate_bps":  "audio_bitrate_bps",
                })
                _apply("retry",  {
                    "max_attempts":    "retry_max_attempts",
                    "initial_delay_s": "retry_initial_delay_s",
                    "max_delay_s":     "retry_max_delay_s",
                    "backoff_factor":  "retry_backoff_factor",
                })
                _apply("logging", {
                    "level":     "log_level",
                    "gst_debug": "log_gst_debug",
                })

    # CLI overrides (only set if not None)
    for attr, val in overrides.items():
        if val is not None:
            setattr(cfg, attr, val)

    return cfg


# =============================================================================
# PIPELINE BUILDER
# =============================================================================

ENCODER_PROFILES = {
    "x264": {
        "encoder":  (
            "x264enc name=venc "
            "tune=zerolatency "
            "speed-preset=ultrafast "
            "bitrate={bitrate} "
            "key-int-max={keyframe_interval} "
        ),
        "caps":    "video/x-h264,profile=constrained-baseline",
        "pay":     "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=102",
    },
    "vaapi": {
        "encoder":  (
            "vaapih264enc name=venc "
            "bitrate={bitrate} "
            "keyframe-period={keyframe_interval} "
            "rate-control=cbr "
            "quality-level=1 "
        ),
        "caps":    "video/x-h264,profile=constrained-baseline",
        "pay":     "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=102",
    },
    "nvenc": {
        "encoder":  (
            "nvh264enc name=venc "
            "bitrate={bitrate} "
            "gop-size={keyframe_interval} "
            "preset=low-latency-hq "
            "rc-mode=cbr "
        ),
        "caps":    "video/x-h264,profile=constrained-baseline",
        "pay":     "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=102",
    },
}


def build_pipeline_string(cfg: Config) -> str:
    """
    Build the GStreamer pipeline string.

    Pipeline topology:
      ndisrc → ndidemux ─┬─ [video path] → rtph264pay → whipclientsink
                          └─ [audio path] → rtpopuspay ↗

    ndisrc / ndidemux notes:
      The gst-plugins-rs NDI plugin (video/ndi/) outputs a raw NDI buffer
      via ndisrc; ndidemux splits it into separate video/audio src pads.
      Pad names vary by plugin version — see --probe-pads flag.
    """
    enc_profile = ENCODER_PROFILES.get(cfg.video_encoder)
    if enc_profile is None:
        raise ValueError(
            f"Unknown video_encoder '{cfg.video_encoder}'. "
            f"Valid: {list(ENCODER_PROFILES)}"
        )

    encoder_str = enc_profile["encoder"].format(
        bitrate=cfg.video_bitrate_kbps,
        keyframe_interval=cfg.keyframe_interval,
    )
    encoder_caps = enc_profile["caps"]
    rtp_pay_str  = enc_profile["pay"]

    video_caps = (
        f"video/x-raw,format=I420"
        f",width={cfg.video_width}"
        f",height={cfg.video_height}"
        f",framerate={cfg.video_framerate}/1"
    )

    audio_caps = (
        f"audio/x-raw"
        f",rate={cfg.audio_sample_rate}"
        f",channels={cfg.audio_channels}"
        f",format=S16LE"
        f",layout=interleaved"
    )

    # Conditional auth-token property (child property of signaller in 1.24+)
    auth_prop = f'signaller::auth-token="{cfg.auth_token}"' if cfg.auth_token else ""

    # Conditional STUN/TURN
    stun_prop = f'stun-server="{cfg.stun_server}"' if cfg.stun_server else ""
    turn_prop = f'turn-server="{cfg.turn_server}"' if cfg.turn_server else ""

    # ─── Pipeline string ────────────────────────────────────────────────────
    #
    # IMPORTANT: If your NDI plugin version exposes separate pads with different
    # names (e.g. video_0 / audio_0, or just unnamed request pads), adjust the
    # "demux.video" / "demux.audio" references below.
    # Run:  gst-inspect-1.0 ndidemux  to see actual pad template names.
    #
    # Alternative for plugins with ndivideosrc / ndiaudiosrc:
    #   Replace the ndisrc+ndidemux block with:
    #     ndivideosrc ndi-name="..." ! ...
    #     ndiaudiosrc ndi-name="..." ! ...
    #
    pipeline = f"""
        whipclientsink name=whip
            signaller::whip-endpoint="{cfg.whip_url}"
            {auth_prop}
            {stun_prop}
            {turn_prop}
            async-handling=true

        ndisrc name=ndi_src
            ndi-name="{cfg.ndi_source_name}"
            connect-timeout={cfg.ndi_connect_timeout_ms}
            do-timestamp=true
        ! ndidemux name=demux

        demux.video
        ! queue name=vqueue
            leaky=downstream
            max-size-buffers=5
            max-size-time=0
            max-size-bytes=0
        ! videoconvert
        ! videoscale
        ! videorate
        ! {video_caps}
        ! {encoder_str}
        ! {encoder_caps}
        ! whip.

        demux.audio
        ! queue name=aqueue
            leaky=downstream
            max-size-buffers=10
            max-size-time=0
            max-size-bytes=0
        ! audioconvert
        ! audioresample
        ! {audio_caps}
        ! opusenc name=aenc
            bitrate={cfg.audio_bitrate_bps}
            audio-type=restricted-lowdelay
            bandwidth=fullband
            frame-size=10
        ! whip.
    """

    # Collapse extra whitespace for clean logging
    return " ".join(pipeline.split())


# =============================================================================
# NDI SOURCE PROBE
# =============================================================================

def probe_ndi_sources(timeout_s: float = 5.0) -> list[str]:
    """
    Enumerate visible NDI sources using the ndideviceprovider GStreamer
    device provider (available in gst-plugins-rs >= 1.24).
    Returns a list of source display-name strings.
    """
    sources: list[str] = []

    try:
        factory = Gst.DeviceProviderFactory.find("ndideviceprovider")
        if factory is None:
            log.error("probe_failed", reason="ndideviceprovider not available")
            return sources

        provider = factory.get()
        provider.start()
        time.sleep(timeout_s)
        for device in provider.get_devices():
            name = device.get_display_name()
            if name:
                sources.append(name)
        provider.stop()
    except Exception as exc:
        log.warning("probe_error", exc=str(exc))

    return sources


# =============================================================================
# BRIDGE CLASS
# =============================================================================

class NdiToWhipBridge:
    """
    Manages the GStreamer pipeline lifecycle, including startup, shutdown,
    error recovery, and reconnect logic.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg          = cfg
        self.pipeline: Optional[Gst.Pipeline] = None
        self.loop: Optional[GLib.MainLoop]    = None
        self._stop_event   = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._attempt      = 0

    # ── Pipeline construction ─────────────────────────────────────────────────

    def _create_pipeline(self) -> Gst.Pipeline:
        pipeline_str = build_pipeline_string(self.cfg)
        log.debug("pipeline_string", pipeline=pipeline_str)

        try:
            pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as exc:
            raise RuntimeError(
                f"Failed to parse GStreamer pipeline: {exc}\n"
                "Hint: run with --print-pipeline and test manually with gst-launch-1.0"
            ) from exc

        if not isinstance(pipeline, Gst.Pipeline):
            pipeline = Gst.Pipeline.new("ndi_to_whip")
            # parse_launch returned a bin; wrap it
            pipeline.add(Gst.parse_launch(pipeline_str))

        return pipeline

    # ── Bus message handler ───────────────────────────────────────────────────

    def _on_bus_message(self, bus: Gst.Bus, msg: Gst.Message) -> bool:   # noqa: ARG002
        t = msg.type

        if t == Gst.MessageType.EOS:
            log.warning("pipeline_eos", hint="NDI source ended or WHIP server closed stream")
            self._schedule_reconnect()

        elif t == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.error(
                "pipeline_error",
                src=msg.src.get_name() if msg.src else "unknown",
                error=err.message,
                debug=debug,
            )
            self._schedule_reconnect()

        elif t == Gst.MessageType.WARNING:
            warn, debug = msg.parse_warning()
            log.warning(
                "pipeline_warning",
                src=msg.src.get_name() if msg.src else "unknown",
                warning=warn.message,
                debug=debug,
            )

        elif t == Gst.MessageType.STATE_CHANGED:
            if msg.src == self.pipeline:
                old, new, pending = msg.parse_state_changed()
                log.info(
                    "state_change",
                    old=old.value_nick,
                    new=new.value_nick,
                    pending=pending.value_nick,
                )

        elif t == Gst.MessageType.ELEMENT:
            structure = msg.get_structure()
            if structure:
                name = structure.get_name()
                # WHIP signalling events emitted by whipclientsink
                if name in ("whip-connected", "whip-error", "whip-disconnected"):
                    log.info("whip_event", event=name, detail=structure.to_string())

        return True  # keep watching

    # ── Reconnect logic ───────────────────────────────────────────────────────

    def _schedule_reconnect(self) -> None:
        """Stop current pipeline and signal the run loop to reconnect."""
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.loop and self.loop.is_running():
            self.loop.quit()

    def _reconnect_delay(self) -> float:
        delay = min(
            self.cfg.retry_initial_delay_s
            * (self.cfg.retry_backoff_factor ** max(0, self._attempt - 1)),
            self.cfg.retry_max_delay_s,
        )
        return delay

    # ── GLib loop in background thread ────────────────────────────────────────

    def _run_glib_loop(self) -> None:
        self.loop = GLib.MainLoop()
        self.loop.run()

    # ── Single pipeline run ───────────────────────────────────────────────────

    def _run_once(self) -> None:
        """Start a single pipeline run; returns when the pipeline ends/errors."""
        self._attempt += 1
        log.info("pipeline_start", attempt=self._attempt, ndi=self.cfg.ndi_source_name)

        self.pipeline = self._create_pipeline()

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        # Start GLib loop in background thread
        self._loop_thread = threading.Thread(target=self._run_glib_loop, daemon=True)
        self._loop_thread.start()

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.error("pipeline_start_failed", hint="Check NDI source name and WHIP URL")
            self._schedule_reconnect()

        # Wait for loop to exit (due to error, EOS, or stop signal)
        self._loop_thread.join()
        log.info("pipeline_stopped", attempt=self._attempt)

        # Cleanup
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    # ── Main public API ───────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the bridge with retry/reconnect until stopped."""
        log.info(
            "bridge_starting",
            ndi_source=self.cfg.ndi_source_name,
            whip_url=self.cfg.whip_url,
            encoder=self.cfg.video_encoder,
            resolution=f"{self.cfg.video_width}x{self.cfg.video_height}@{self.cfg.video_framerate}",
            video_bitrate_kbps=self.cfg.video_bitrate_kbps,
            audio_bitrate_bps=self.cfg.audio_bitrate_bps,
        )

        max_attempts = self.cfg.retry_max_attempts

        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception as exc:
                log.exception("pipeline_exception", exc=str(exc))

            if self._stop_event.is_set():
                break

            if max_attempts and self._attempt >= max_attempts:
                log.error("retry_limit_reached", attempts=self._attempt)
                break

            delay = self._reconnect_delay()
            log.info("reconnect_waiting", delay_s=round(delay, 1), attempt=self._attempt)

            # Interruptible sleep
            self._stop_event.wait(timeout=delay)

        log.info("bridge_stopped")

    def stop(self) -> None:
        """Signal a clean shutdown."""
        log.info("stop_requested")
        self._stop_event.set()
        self._schedule_reconnect()


# =============================================================================
# CLI / ENTRY POINT
# =============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="NDI → WHIP bridge (GStreamer-based)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config",          metavar="PATH",
                   help="Path to TOML config file")
    p.add_argument("--probe",           action="store_true",
                   help="Discover and print visible NDI sources, then exit")
    p.add_argument("--probe-timeout",   type=float, default=5.0, metavar="S",
                   help="Seconds to wait while probing for NDI sources")
    p.add_argument("--print-pipeline",  action="store_true",
                   help="Print the generated GStreamer pipeline string, then exit")
    p.add_argument("--validate",        action="store_true",
                   help="Check that all required GStreamer elements exist, then exit")

    # Overrides (all optional — TOML file takes precedence if not given)
    g = p.add_argument_group("overrides (take precedence over config file)")
    g.add_argument("--ndi-source",      metavar="NAME",  dest="ndi_source_name")
    g.add_argument("--whip-url",        metavar="URL",   dest="whip_url")
    g.add_argument("--auth-token",      metavar="TOKEN", dest="auth_token")
    g.add_argument("--width",           type=int,        dest="video_width")
    g.add_argument("--height",          type=int,        dest="video_height")
    g.add_argument("--framerate",       type=int,        dest="video_framerate")
    g.add_argument("--video-bitrate",   type=int,        dest="video_bitrate_kbps",
                   metavar="KBPS")
    g.add_argument("--encoder",         choices=list(ENCODER_PROFILES),
                   dest="video_encoder")
    g.add_argument("--audio-bitrate",   type=int,        dest="audio_bitrate_bps",
                   metavar="BPS")
    g.add_argument("--log-level",       dest="log_level",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def validate_elements() -> bool:
    required = [
        "ndisrc", "ndidemux",
        "whipclientsink",
        "videoconvert", "videoscale", "videorate",
        "x264enc", "opusenc",
        "rtph264pay", "rtpopuspay",
        "audioconvert", "audioresample",
        "queue",
    ]
    all_ok = True
    for name in required:
        factory = Gst.ElementFactory.find(name)
        status  = "OK" if factory else "MISSING"
        if not factory:
            all_ok = False
        print(f"  {'✓' if factory else '✗'}  {name:25s}  {status}")
    return all_ok


def main() -> None:
    args = parse_args()

    # Build overrides dict (only non-None values)
    overrides = {k: v for k, v in vars(args).items()
                 if k not in {"config", "probe", "probe_timeout",
                              "print_pipeline", "validate"}
                 and v is not None}

    cfg = load_config(args.config, overrides)

    # Configure GStreamer debug level
    os.environ.setdefault("GST_DEBUG", cfg.log_gst_debug)
    os.environ.setdefault("GST_PLUGIN_PATH",
                          "/usr/local/lib/gstreamer-1.0")
    os.environ.setdefault("LD_LIBRARY_PATH",
                          "/usr/local/lib:" + os.environ.get("LD_LIBRARY_PATH", ""))

    # Configure Python log level
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(level)

    # ── Modes ──────────────────────────────────────────────────────────────────

    if args.validate:
        print("Validating required GStreamer elements:")
        ok = validate_elements()
        sys.exit(0 if ok else 1)

    if args.probe:
        print(f"Probing for NDI sources ({args.probe_timeout}s)…")
        sources = probe_ndi_sources(timeout_s=args.probe_timeout)
        if sources:
            print(f"\nFound {len(sources)} NDI source(s):")
            for s in sources:
                print(f"  • {s}")
        else:
            print("  No NDI sources found. Check network and NDI sender.")
        sys.exit(0)

    if args.print_pipeline:
        print(build_pipeline_string(cfg))
        sys.exit(0)

    # ── Streaming mode ─────────────────────────────────────────────────────────

    bridge = NdiToWhipBridge(cfg)

    def _sig_handler(signum: int, _frame) -> None:  # type: ignore[type-arg]
        sig_name = signal.Signals(signum).name
        log.info("signal_received", signal=sig_name)
        bridge.stop()

    signal.signal(signal.SIGINT,  _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    bridge.run()


if __name__ == "__main__":
    main()
