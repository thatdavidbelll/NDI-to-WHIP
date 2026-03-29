# NDI → WHIP Bridge

Production-grade Ubuntu service that captures a named NDI source and publishes it
to a WHIP (WebRTC HTTP Ingest Protocol) endpoint as H.264 + Opus over WebRTC.

Built on GStreamer with `whipclientsink` (from `gst-plugins-rs`) and the NDI plugin
from the same Rust workspace. Single pipeline, no intermediate hops, low latency.

---

## Contents

- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
  - [1. System packages](#1-system-packages)
  - [2. Rust toolchain](#2-rust-toolchain)
  - [3. NDI SDK (manual)](#3-ndi-sdk-manual)
  - [4. Build GStreamer plugins](#4-build-gstreamer-plugins)
  - [5. Application setup](#5-application-setup)
- [Configuration](#configuration)
- [Running](#running)
  - [Manual / foreground](#manual--foreground)
  - [As a systemd service](#as-a-systemd-service)
- [Testing and Validation](#testing-and-validation)
- [Troubleshooting](#troubleshooting)
- [Hardware Encoding](#hardware-encoding)
- [Known Limitations](#known-limitations)

---

## Architecture

```
NDI network
    │
    ▼
ndisrc  ──►  ndidemux
                │
                ├── video ──► videoconvert ► videoscale ► videorate
                │             ► x264enc (zerolatency, CBR)
                │             ► rtph264pay
                │                              ┐
                └── audio ──► audioconvert      ├──► whipclientsink
                              ► audioresample   │    (WHIP HTTP POST)
                              ► opusenc         │    (ICE + DTLS-SRTP)
                              ► rtpopuspay      ┘
                                                │
                                        WHIP ingest endpoint
```

**Why GStreamer over FFmpeg+relay:**

- Single pipeline — no intermediate mux/demux hop, lowest achievable latency
- `whipclientsink` is the only production-grade open-source WHIP *client* on Linux
- Both the NDI and WebRTC plugins live in `gst-plugins-rs` — same build, same GStreamer version
- `queue leaky=downstream` on both paths prevents back-pressure from stalling NDI receive
- `constrained-baseline` H.264 profile is accepted by every WHIP server without SDP surprises

---

## Requirements

| Component | Minimum version | Notes |
|---|---|---|
| Ubuntu | 22.04 LTS | 24.04 preferred (newer GStreamer) |
| GStreamer | 1.20 | 1.22+ preferred |
| Rust / Cargo | stable | Only needed at build time |
| NDI SDK | v5 or v6 | Proprietary — download from ndi.tv |
| Python | 3.10 | 3.11+ gets tomllib without extra install |
| libnice | any | Ships with Ubuntu — ICE for WebRTC |
| libsrtp2 | any | Ships with Ubuntu — SRTP |
| libx264 | any | Ships with Ubuntu — software H.264 |
| libopus | any | Ships with Ubuntu — Opus audio |

---

## Installation

> Run all steps on the target Ubuntu server. Steps 1–4 require root or sudo.

### 1. System packages

```bash
sudo apt update
sudo apt install -y \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  gstreamer1.0-nice \
  libgstreamer1.0-dev \
  libgstreamer-plugins-base1.0-dev \
  libgstreamer-plugins-bad1.0-dev \
  libnice-dev \
  libssl-dev \
  libsrtp2-dev \
  libx264-dev \
  libopus-dev \
  build-essential pkg-config git curl nasm \
  python3 python3-pip python3-venv python3-gi \
  gir1.2-gstreamer-1.0 \
  gir1.2-gst-plugins-base-1.0
```

**Ubuntu 22.04 note:** Ships GStreamer 1.20. If you hit API errors during the Rust
build, install a newer GStreamer via the developers PPA:

```bash
sudo add-apt-repository ppa:gstreamer-developers/ppa
sudo apt update && sudo apt upgrade
```

Or upgrade to Ubuntu 24.04 (ships GStreamer 1.24, recommended for new deployments).

---

### 2. Rust toolchain

Rust is required to build `gst-plugins-rs`. It is only needed at build time.

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source ~/.cargo/env   # or open a new shell

cargo --version       # verify
```

For an unattended server install (system-wide):

```bash
export CARGO_HOME=/opt/cargo
export RUSTUP_HOME=/opt/rustup
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
  | sh -s -- -y --no-modify-path --profile minimal
export PATH="/opt/cargo/bin:$PATH"
```

---

### 3. NDI SDK (manual)

The NDI SDK is proprietary software that requires accepting a licence agreement.
It cannot be downloaded automatically.

**Steps:**

1. Go to **https://www.ndi.tv/sdk/** and create a free account
2. Download the Linux installer — look for a file like `Install_NDI_SDK_v6_Linux.tar.gz`
3. Extract and run the installer:

```bash
tar -xzf Install_NDI_SDK_v6_Linux.tar.gz
sudo bash Install_NDI_SDK_v6_Linux.sh
# Accept the EULA, choose install path /usr/local (the default)
sudo ldconfig
```

4. Verify:

```bash
ls /usr/local/include/Processing.NDI.Lib.h   # must exist
ls /usr/local/lib/libndi.so*                 # must exist
```

---

### 4. Build GStreamer plugins

This builds `libgstndi.so` (NDI source/sink) and `libgstwebrtc.so` (WHIP client)
from the `gst-plugins-rs` Rust workspace.

```bash
git clone --branch gstreamer-1.24.13 --depth 1 \
  https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git
cd gst-plugins-rs

# Build both plugins (takes 5–15 minutes on first run)
NDI_SDK_DIR=/usr/local cargo build --release \
  --package gst-plugin-ndi \
  --package gst-plugin-webrtc

# Install to the system GStreamer plugin directory
sudo install -m 755 target/release/libgstndi.so    /usr/local/lib/gstreamer-1.0/
sudo install -m 755 target/release/libgstwebrtc.so /usr/local/lib/gstreamer-1.0/
sudo ldconfig
```

> **Note:** The system package `gstreamer1.0-plugins-bad` installs a *different*
> `libgstwebrtc.so` that provides `webrtcbin` but NOT `whipclientsink`. The file
> installed above from `gst-plugins-rs` must take precedence. Set
> `GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0` to ensure the right one loads.

**Verify both elements are available:**

```bash
export GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0
gst-inspect-1.0 ndisrc          # must print element details
gst-inspect-1.0 whipclientsink  # must print element details
```

---

### 5. Application setup

```bash
# Create service user
sudo useradd --system --no-create-home --shell /usr/sbin/nologin ndi-whip
sudo usermod -aG video,audio ndi-whip

# Create directories
sudo mkdir -p /opt/ndi_to_whip /etc/ndi_to_whip /var/log/ndi_to_whip

# Copy application files (from this repo)
sudo cp ndi_to_whip.py /opt/ndi_to_whip/
sudo cp config.toml    /etc/ndi_to_whip/
sudo chmod +x /opt/ndi_to_whip/ndi_to_whip.py
sudo chmod 640 /etc/ndi_to_whip/config.toml

# Python virtual environment
sudo python3 -m venv /opt/ndi_to_whip/venv
sudo /opt/ndi_to_whip/venv/bin/pip install --upgrade pip
sudo /opt/ndi_to_whip/venv/bin/pip install tomli structlog

# Fix ownership
sudo chown -R ndi-whip:ndi-whip /opt/ndi_to_whip /var/log/ndi_to_whip

# Persist environment for all shells
echo '/usr/local/lib' | sudo tee /etc/ld.so.conf.d/ndi.conf
sudo ldconfig

# Install systemd service
sudo cp ndi-to-whip.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Or run the automated installer (still requires NDI SDK to already be installed):

```bash
sudo bash install.sh
```

---

## Configuration

Edit `/etc/ndi_to_whip/config.toml` before starting the service.
Every field is documented inline in the file. Key settings:

```toml
[ndi]
source_name = "MYCOMPUTER (Stream 1)"   # exact NDI source name

[whip]
url        = "https://your-whip-endpoint/whip"
auth_token = "your-bearer-token"        # leave "" if not required
stun_server = "stun://stun.l.google.com:19302"

[video]
width        = 1920
height       = 1080
framerate    = 30
bitrate_kbps = 4000
encoder      = "x264"    # x264 | vaapi | nvenc

[audio]
channels    = 2
sample_rate = 48000
bitrate_bps = 128000
```

### WHIP endpoint examples

| Service | URL format |
|---|---|
| Cloudflare Stream | `https://customer-<id>.cloudflarestream.com/<stream-key>/webRTC/publish` |
| LiveKit | `https://your-livekit-host/whip` |
| Janus (local) | `http://127.0.0.1:8088/janus/whip` |
| MediaMTX (local) | `http://127.0.0.1:8889/<stream-name>/whip` |

---

## Running

### Manual / foreground

```bash
# Discover available NDI sources on the network
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py --probe

# Print the generated GStreamer pipeline (for manual testing)
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py \
  --config /etc/ndi_to_whip/config.toml --print-pipeline

# Run interactively with config file
GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
LD_LIBRARY_PATH=/usr/local/lib \
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py \
  --config /etc/ndi_to_whip/config.toml

# Override any config value on the command line
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py \
  --ndi-source "MYPC (Camera 1)" \
  --whip-url "http://127.0.0.1:8889/live/whip" \
  --width 1280 --height 720 --framerate 30 \
  --video-bitrate 2500 \
  --log-level DEBUG
```

Stop with `Ctrl+C` — triggers a clean SIGINT shutdown.

### As a systemd service

```bash
# Enable and start
sudo systemctl enable --now ndi-to-whip

# Check status
sudo systemctl status ndi-to-whip

# Follow live logs
sudo journalctl -u ndi-to-whip -f

# Restart after a config change
sudo systemctl restart ndi-to-whip

# Stop
sudo systemctl stop ndi-to-whip

# View last 200 log lines
sudo journalctl -u ndi-to-whip -n 200 --no-pager

# Logs since a specific time
sudo journalctl -u ndi-to-whip --since "2024-01-15 10:00:00"
```

---

## Testing and Validation

### Pre-flight checklist

```bash
# Runs ~20 automated checks and prints pass/warn/fail for each
sudo bash validate.sh

# Skip the 5-second NDI network scan
bash validate.sh --quick
```

### Validate GStreamer elements only

```bash
GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
/opt/ndi_to_whip/venv/bin/python /opt/ndi_to_whip/ndi_to_whip.py --validate
```

### Test the raw GStreamer pipeline manually

Copy the output of `--print-pipeline` and run it directly with `gst-launch-1.0`.
Add `GST_DEBUG=whipclientsink:4` to see the WHIP HTTP exchange:

```bash
GST_DEBUG=whipclientsink:4,ndisrc:3 \
GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
LD_LIBRARY_PATH=/usr/local/lib \
gst-launch-1.0 \
  whipclientsink name=whip \
    sig-server-url="http://127.0.0.1:8889/live/whip" \
  ndisrc ndi-name="SOURCENAME" ! ndidemux name=d \
  d.video ! queue leaky=downstream ! videoconvert ! videoscale ! videorate ! \
    "video/x-raw,format=I420,width=1280,height=720,framerate=30/1" ! \
    x264enc tune=zerolatency speed-preset=ultrafast bitrate=2500 key-int-max=60 ! \
    "video/x-h264,profile=constrained-baseline" ! \
    rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=102 ! whip. \
  d.audio ! queue leaky=downstream ! audioconvert ! audioresample ! \
    "audio/x-raw,rate=48000,channels=2" ! \
    opusenc bitrate=128000 ! rtpopuspay pt=111 ! whip.
```

### Test WHIP signalling independently

```bash
# A 201 Created response confirms the WHIP endpoint accepts your token and SDP
curl -v -X POST \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/sdp" \
  -d "v=0
o=- 0 0 IN IP4 127.0.0.1
s=-
t=0 0
m=video 9 UDP/TLS/RTP/SAVPF 102
c=IN IP4 0.0.0.0
a=rtpmap:102 H264/90000
m=audio 9 UDP/TLS/RTP/SAVPF 111
c=IN IP4 0.0.0.0
a=rtpmap:111 opus/48000/2" \
  "https://YOUR_WHIP_ENDPOINT"
```

### Verify NDI is visible on the network

```bash
# Check multicast traffic from NDI senders
sudo tcpdump -i eth0 -n udp port 5353

# Check NDI UDP port range
sudo tcpdump -i eth0 -n 'udp portrange 5960-5970'
```

### Monitor performance

```bash
# CPU usage
top -p $(pgrep -f ndi_to_whip.py)

# Memory
cat /proc/$(pgrep -f ndi_to_whip.py)/status | grep VmRSS

# GStreamer performance counters
GST_DEBUG="GST_PERFORMANCE:5" /opt/ndi_to_whip/venv/bin/python ...

# Encoder-specific debug (dropped frames etc.)
GST_DEBUG="x264enc:4,videorate:4" /opt/ndi_to_whip/venv/bin/python ...
```

---

## Troubleshooting

### NDI source not found / `--probe` returns nothing

```
No NDI sources found.
```

NDI uses mDNS (UDP/5353) for discovery and UDP/5960 for video/audio. These must be
unblocked at the network and firewall level.

```bash
# Open ports on ufw
sudo ufw allow 5353/udp
sudo ufw allow 5960

# Verify multicast works on the interface
ip route show | grep '224.0.0.0'
# If missing:
sudo ip route add 224.0.0.0/4 dev eth0

# Force unicast discovery by specifying the sender's IP
# In config.toml: source_name = "192.168.1.50 (Stream Name)"
```

NDI discovery also fails across VLANs unless multicast is explicitly routed. If your
NDI sender is on a different VLAN, consult your switch documentation or configure the
sender to use a static unicast address.

---

### `ndisrc` or `ndidemux` element not available

```
(gst) No such element or plugin 'ndisrc'
```

The plugin file is not in `GST_PLUGIN_PATH` or failed to load.

```bash
# Confirm the file exists
ls -lh /usr/local/lib/gstreamer-1.0/libgstndi.so

# Check it loads cleanly
gst-inspect-1.0 --print-all 2>&1 | grep -i ndi

# Check for missing shared library dependencies
ldd /usr/local/lib/gstreamer-1.0/libgstndi.so | grep "not found"
# If libndi.so is missing:
sudo ldconfig
# If still missing:
ls /usr/local/lib/libndi.so*   # confirm NDI SDK is installed
```

---

### `whipclientsink` not found (wrong plugin loaded)

```
(gst) No such element 'whipclientsink'
# OR gst-inspect shows webrtcbin info instead
```

The system's `libgstwebrtc.so` from `gstreamer1.0-plugins-bad` is loading instead
of the one built from `gst-plugins-rs`.

```bash
# Force the correct plugin directory
export GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0

# Verify which file is being loaded
GST_DEBUG=3 gst-inspect-1.0 whipclientsink 2>&1 | grep "loaded"

# If the system version keeps winning, rename it
sudo mv /usr/lib/x86_64-linux-gnu/gstreamer-1.0/libgstwebrtc.so \
        /usr/lib/x86_64-linux-gnu/gstreamer-1.0/libgstwebrtc.so.disabled
```

---

### WHIP HTTP 401 / 403

```
GST_DEBUG output: HTTP 401 Unauthorized  or  403 Forbidden
```

- Verify `auth_token` in config is correct and not expired
- Some WHIP servers expect the token as a URL query param (`?access_token=...`)
  rather than an `Authorization: Bearer` header — check your server's docs
- For Cloudflare Stream: use the **stream key**, not the account API token

---

### ICE fails — signalling succeeds but no media arrives

```
whip-connected fires, stream never appears at receiver
```

Direct UDP is being blocked. You need a TURN relay.

```bash
# Test whether outbound UDP works at all
nc -uzv stun.l.google.com 19302    # should succeed

# Add a TURN server to config.toml:
# turn_server = "turn://username:password@your-coturn-server:3478"

# Install coturn locally if needed
sudo apt install coturn
```

---

### `ndidemux` pad names mismatch — pipeline fails to link

```
(gst) could not link ndidemux0 to queue
```

Different plugin versions expose different pad template names. Find yours:

```bash
gst-inspect-1.0 ndidemux
# Look for "SRC template" entries — note the pad name pattern
```

Then edit `build_pipeline_string()` in `ndi_to_whip.py` and change `demux.video` /
`demux.audio` to match. Common alternatives: `demux.src_0` / `demux.src_1`, or
unnamed request pads.

If you want a more resilient alternative, replace the `ndisrc ! ndidemux` block with
`decodebin`, which auto-negotiates pad types:

```python
# In ndi_to_whip.py, change the pipeline block to:
"ndisrc ndi-name='...' ! decodebin name=dec"
# Then use "dec." for both video and audio chains
```

---

### High CPU / dropped frames

```
GST videorate: dropped N frames
top shows 100% CPU on a single core
```

Software H.264 encoding is CPU-bound. Options in order of preference:

1. Switch to hardware encoding — see [Hardware Encoding](#hardware-encoding)
2. Lower resolution: `width=1280, height=720`
3. Lower framerate: `framerate=25` or `framerate=24`
4. Lower bitrate: `bitrate_kbps=2000`
5. Use `speed-preset=superfast` instead of `ultrafast` (slightly better quality,
   similar CPU cost)

---

### Service restarts in a tight loop

```
journalctl shows repeated start/stop within seconds
```

Check the exit reason:

```bash
sudo journalctl -u ndi-to-whip -n 50 --no-pager | grep -E "ERROR|WARN|Failed"
```

Common causes:
- NDI source name is wrong — run `--probe` to get the exact string
- WHIP URL is unreachable — `curl -v YOUR_WHIP_URL`
- `config.toml` has a syntax error — `python -c "import tomllib; tomllib.load(open('/etc/ndi_to_whip/config.toml','rb'))"`
- Permission denied on log dir — `ls -la /var/log/ndi_to_whip/`

Increase retry delay to slow the loop while debugging:
```toml
[retry]
initial_delay_s = 15.0
```

---

## Hardware Encoding

Software `x264enc` is the default because it works everywhere without extra
dependencies. For production on real hardware, switch to GPU encoding.

### Intel / AMD — VAAPI

```bash
# Verify VA-API support
sudo apt install vainfo
vainfo   # must show VAProfileH264ConstrainedBaseline ... VAEntrypointEncSlice

# Enable in config.toml
# encoder = "vaapi"

# Add service user to render group (Intel iGPU)
sudo usermod -aG render ndi-whip
```

### NVIDIA — NVENC

```bash
# Verify NVENC support
nvidia-smi
gst-inspect-1.0 nvh264enc   # must print element info

# Install CUDA and NVENC GStreamer plugin if missing
sudo apt install gstreamer1.0-plugins-bad  # contains nvh264enc on NVIDIA systems

# Enable in config.toml
# encoder = "nvenc"
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|---|---|---|
| NDI SDK is proprietary | Manual install required | Script fails clearly and prints download URL |
| `gst-plugins-rs` requires Rust build | No Ubuntu package; 5–15 min build | Build once, pin git tag, cache `target/` |
| `ndidemux` pad names vary by plugin version | Pipeline may fail to link | Use `gst-inspect-1.0 ndidemux` to find pad names; adjust pipeline string |
| H.264 constrained-baseline only | No B-frames, capped quality | Sufficient for live broadcast; VP9 profile can be added |
| WHIP ICE without TURN fails behind symmetric NAT | No media after signalling | Add TURN server to `[whip] turn_server` |
| No hardware encoding on VMs without GPU passthrough | High CPU at 1080p | Use x264 at 720p30, or add GPU passthrough |
| Python GIL + GLib main loop threading | Minor risk of deadlock on unusual errors | `_loop_thread` is a daemon thread; SIGTERM always terminates |
| NDI SDK licence restricts redistribution | Cannot package libndi.so in a .deb | Automate install post-SDK-download only |

---

## File Reference

```
ndi_to_whip/
├── install.sh            Full system installer (run as root)
├── ndi_to_whip.py        Main Python application
├── config.toml           Configuration — edit before first run
├── ndi-to-whip.service   systemd unit file
├── validate.sh           Pre-flight checklist and troubleshooting
└── README.md             This file
```
