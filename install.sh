#!/usr/bin/env bash
# =============================================================================
# install.sh — NDI → WHIP bridge: full system installation
#
# Targets: Ubuntu 22.04 LTS (jammy) / Ubuntu 24.04 LTS (noble)
# Run as: sudo bash install.sh
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
die()   { echo -e "${RED}[FAIL]${NC}  $*" >&2; exit 1; }

# ── Script location (must be resolved before any cd) ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Config ────────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/ndi_to_whip"
GST_PLUGIN_DIR="/usr/local/lib/gstreamer-1.0"
NDI_SDK_SYMLINK_DIR="/usr/local"   # where NDI SDK installs its headers + libs
GST_PLUGINS_RS_REV="gstreamer-1.24.13"   # git tag / branch to build
PYTHON_VENV="${INSTALL_DIR}/venv"
SERVICE_USER="ndi-whip"
LOG_DIR="/var/log/ndi_to_whip"
CONFIG_DIR="/etc/ndi_to_whip"

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run this script as root: sudo bash install.sh"

UBUNTU_VERSION=$(lsb_release -rs 2>/dev/null || echo "unknown")
info "Detected Ubuntu ${UBUNTU_VERSION}"

# =============================================================================
# 1. SYSTEM PACKAGES
# =============================================================================
info "Installing system packages…"
apt-get update -qq

# GStreamer core + plugins
GST_PKGS=(
  gstreamer1.0-tools
  gstreamer1.0-plugins-base
  gstreamer1.0-plugins-good
  gstreamer1.0-plugins-bad
  gstreamer1.0-plugins-ugly
  gstreamer1.0-libav
  gstreamer1.0-nice                 # libnice — ICE for WebRTC
  libgstreamer1.0-dev
  libgstreamer-plugins-base1.0-dev
  libgstreamer-plugins-bad1.0-dev
  libnice-dev
  libssl-dev                        # DTLS via OpenSSL
  libsrtp2-dev                      # SRTP
)

# Build toolchain
BUILD_PKGS=(
  build-essential
  pkg-config
  cmake
  ninja-build
  meson
  git
  curl
  wget
  ca-certificates
  nasm                              # x264 dependency
)

# Python runtime
PY_PKGS=(
  python3
  python3-pip
  python3-venv
  python3-gi
  python3-gi-cairo
  gir1.2-gstreamer-1.0
  gir1.2-gst-plugins-base-1.0
)

# Codec libraries
CODEC_PKGS=(
  libx264-dev
  libopus-dev
)

apt-get install -y --no-install-recommends \
  "${GST_PKGS[@]}" \
  "${BUILD_PKGS[@]}" \
  "${PY_PKGS[@]}" \
  "${CODEC_PKGS[@]}"

ok "System packages installed."

# =============================================================================
# 2. RUST TOOLCHAIN (needed to build gst-plugins-rs)
# =============================================================================
if ! command -v cargo &>/dev/null; then
  info "Installing Rust toolchain via rustup…"
  # Install for root but make available system-wide
  export CARGO_HOME="/opt/cargo"
  export RUSTUP_HOME="/opt/rustup"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --profile minimal
  export PATH="/opt/cargo/bin:$PATH"
  # Persist environment for subsequent shells
  cat >> /etc/environment <<'EOF'
CARGO_HOME=/opt/cargo
RUSTUP_HOME=/opt/rustup
PATH=/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
EOF
else
  info "Rust already installed: $(cargo --version)"
fi

export PATH="/opt/cargo/bin:${PATH}"

ok "Rust toolchain ready."

# =============================================================================
# 3. NDI SDK  (requires manual download — see note below)
# =============================================================================
# The NDI SDK is proprietary. Download the Linux installer from:
#   https://www.ndi.tv/sdk/
# File: Install_NDI_SDK_v6_Linux.tar.gz  (or similar)
#
# After extraction:
#   sudo bash Install_NDI_SDK_v6_Linux.sh
#   sudo ldconfig
#
# The installer places:
#   /usr/local/include/Processing.NDI.Lib.h  (and others)
#   /usr/local/lib/libndi.so.6               (or similar symlink)
#
# This script will verify the SDK is present and fail clearly if not.

NDI_HEADER="/usr/local/include/Processing.NDI.Lib.h"
NDI_LIB_PATTERN="/usr/local/lib/libndi.so*"

if ! ls ${NDI_LIB_PATTERN} &>/dev/null || [[ ! -f "${NDI_HEADER}" ]]; then
  warn "NDI SDK not found at expected paths."
  echo ""
  echo -e "${BOLD}Manual step required:${NC}"
  echo "  1. Download from https://www.ndi.tv/sdk/"
  echo "     (requires free registration)"
  echo "  2. Extract and run:  sudo bash Install_NDI_SDK_v6_Linux.sh"
  echo "  3. Accept the EULA, install to /usr/local"
  echo "  4. Run:  sudo ldconfig"
  echo "  5. Re-run this install script."
  echo ""
  die "NDI SDK must be installed before continuing."
fi

# Locate the actual .so for pkg-config / Cargo
NDI_LIB=$(ls /usr/local/lib/libndi.so* | sort -V | tail -1)
info "NDI SDK found: header=${NDI_HEADER}, lib=${NDI_LIB}"

# Export so Cargo build can find NDI
export NDI_SDK_DIR="/usr/local"
export LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH:-}"
ldconfig

ok "NDI SDK verified."

# =============================================================================
# 4. BUILD gst-plugins-rs  (NDI + WebRTC/WHIP plugins)
# =============================================================================
BUILD_WORK="/tmp/gst-plugins-rs-build"
mkdir -p "${BUILD_WORK}"
cd "${BUILD_WORK}"

if [[ -d gst-plugins-rs ]]; then
  info "gst-plugins-rs source already present — pulling latest…"
  git -C gst-plugins-rs fetch --tags
  git -C gst-plugins-rs checkout "${GST_PLUGINS_RS_REV}" 2>/dev/null || \
    git -C gst-plugins-rs pull
else
  info "Cloning gst-plugins-rs (tag: ${GST_PLUGINS_RS_REV})…"
  git clone --branch "${GST_PLUGINS_RS_REV}" --depth 1 \
    https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git
fi

cd gst-plugins-rs

# Verify GStreamer version compatibility
GST_VERSION=$(gst-launch-1.0 --version 2>&1 | grep -oP '\d+\.\d+\.\d+' | head -1)
info "System GStreamer version: ${GST_VERSION}"
GST_MAJOR=$(echo "${GST_VERSION}" | cut -d. -f1)
GST_MINOR=$(echo "${GST_VERSION}" | cut -d. -f2)
if (( GST_MAJOR < 1 || (GST_MAJOR == 1 && GST_MINOR < 20) )); then
  die "GStreamer >= 1.20 required. Found ${GST_VERSION}. Install from a PPA or upgrade Ubuntu."
fi

# Build NDI plugin (video/ndi) and WebRTC plugin (net/webrtc)
info "Building gst-plugin-ndi…"
NDI_SDK_DIR=/usr/local cargo build \
  --release \
  --package gst-plugin-ndi \
  2>&1 | tail -20

info "Building gst-plugin-webrtc (provides whipclientsink)…"
cargo build \
  --release \
  --package gst-plugin-webrtc \
  2>&1 | tail -20

# Install built plugins to GStreamer plugin path
mkdir -p "${GST_PLUGIN_DIR}"
find target/release -name "libgstndi.so"    -exec install -m 755 {} "${GST_PLUGIN_DIR}/" \;
find target/release -name "libgstrswebrtc.so" -exec install -m 755 {} "${GST_PLUGIN_DIR}/" \;
ldconfig

ok "gst-plugins-rs built and installed."

# =============================================================================
# 5. VERIFY PLUGIN REGISTRATION
# =============================================================================
info "Verifying GStreamer plugin registration…"

export GST_PLUGIN_PATH="${GST_PLUGIN_DIR}"

check_element() {
  local el="$1"
  if gst-inspect-1.0 "${el}" &>/dev/null; then
    ok "  gst element '${el}' is available."
  else
    die "  gst element '${el}' NOT found. Build may have failed."
  fi
}

check_element ndisrc
check_element whipclientsink
check_element x264enc
check_element opusenc
check_element rtph264pay
check_element rtpopuspay

# =============================================================================
# 6. APPLICATION INSTALLATION
# =============================================================================
info "Installing ndi_to_whip application…"

# Create service user (no login, no home)
if ! id "${SERVICE_USER}" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
  ok "Created system user '${SERVICE_USER}'."
fi

# Add service user to video/audio groups if present
for grp in video audio pulse; do
  getent group "${grp}" &>/dev/null && usermod -aG "${grp}" "${SERVICE_USER}" || true
done

mkdir -p "${INSTALL_DIR}" "${LOG_DIR}" "${CONFIG_DIR}"

# Copy application files
cp "${SCRIPT_DIR}/ndi_to_whip.py"  "${INSTALL_DIR}/"
chmod +x "${INSTALL_DIR}/ndi_to_whip.py"

if [[ ! -f "${CONFIG_DIR}/config.toml" ]]; then
  cp "${SCRIPT_DIR}/config.toml" "${CONFIG_DIR}/config.toml"
  chmod 640 "${CONFIG_DIR}/config.toml"
  info "Default config installed at ${CONFIG_DIR}/config.toml — edit before first run."
fi

# Python venv with required packages
info "Setting up Python virtual environment…"
python3 -m venv "${PYTHON_VENV}"
"${PYTHON_VENV}/bin/pip" install --quiet --upgrade pip
"${PYTHON_VENV}/bin/pip" install --quiet \
  tomli \
  structlog

# Set ownership
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}" "${LOG_DIR}"
chmod 750 "${LOG_DIR}"

ok "Application installed to ${INSTALL_DIR}."

# =============================================================================
# 7. SYSTEM ENVIRONMENT PERSISTENCE
# =============================================================================
# Write ld.so config so libndi.so is always found
cat > /etc/ld.so.conf.d/ndi.conf <<'EOF'
/usr/local/lib
EOF
ldconfig

# GStreamer plugin path for the service user
cat > /etc/profile.d/ndi_to_whip.sh <<EOF
export GST_PLUGIN_PATH="${GST_PLUGIN_DIR}"
export LD_LIBRARY_PATH="/usr/local/lib:\${LD_LIBRARY_PATH:-}"
export NDI_SDK_DIR=/usr/local
EOF

ok "Environment persistence configured."

# =============================================================================
# 8. SYSTEMD SERVICE INSTALLATION
# =============================================================================
info "Installing systemd service…"
cp "${SCRIPT_DIR}/ndi-to-whip.service" /etc/systemd/system/
systemctl daemon-reload
ok "systemd service installed (not started yet)."

# =============================================================================
# DONE
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}Installation complete.${NC}"
echo ""
echo "Next steps:"
echo "  1. Edit config:   ${CONFIG_DIR}/config.toml"
echo "  2. Test manually: ${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/ndi_to_whip.py --config ${CONFIG_DIR}/config.toml --probe"
echo "  3. Enable service: systemctl enable --now ndi-to-whip"
echo "  4. Monitor logs:   journalctl -u ndi-to-whip -f"
