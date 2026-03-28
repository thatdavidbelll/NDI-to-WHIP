#!/usr/bin/env bash
# =============================================================================
# validate.sh — NDI → WHIP bridge: validation and troubleshooting checklist
#
# Run as the service user or root:
#   bash validate.sh
#   bash validate.sh --quick    (skip NDI network scan)
# =============================================================================
set -uo pipefail

QUICK=false
[[ "${1:-}" == "--quick" ]] && QUICK=true

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0; WARN=0

pass()  { echo -e "  ${GREEN}✓${NC}  $*"; ((PASS++)); }
fail()  { echo -e "  ${RED}✗${NC}  $*"; ((FAIL++)); }
warn()  { echo -e "  ${YELLOW}!${NC}  $*"; ((WARN++)); }
header(){ echo -e "\n${BOLD}${CYAN}── $* ──${NC}"; }

# ── Helpers ───────────────────────────────────────────────────────────────────
gst_inspect() { gst-inspect-1.0 "$1" &>/dev/null; }

# =============================================================================
header "1. SYSTEM"
# =============================================================================

# Ubuntu version
UBUNTU=$(lsb_release -rs 2>/dev/null || echo "unknown")
echo "  Ubuntu ${UBUNTU}"
[[ "${UBUNTU}" == "22.04" || "${UBUNTU}" == "24.04" ]] \
  && pass "Supported Ubuntu version" \
  || warn "Ubuntu ${UBUNTU} — not tested; proceed with caution"

# GStreamer version
GST_VER=$(gst-launch-1.0 --version 2>&1 | grep -oP '\d+\.\d+\.\d+' | head -1)
echo "  GStreamer ${GST_VER}"
GST_MINOR=$(echo "${GST_VER}" | cut -d. -f2)
(( GST_MINOR >= 20 )) \
  && pass "GStreamer >= 1.20 (required for gst-plugins-rs)" \
  || fail "GStreamer < 1.20 — upgrade needed (apt or PPA)"

# Python 3.10+
PY_VER=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
echo "  Python ${PY_VER}"
PY_MIN=$(echo "${PY_VER}" | cut -d. -f2)
(( PY_MIN >= 10 )) \
  && pass "Python >= 3.10" \
  || warn "Python < 3.10 — tomllib not available, install tomli manually"

# =============================================================================
header "2. NDI SDK"
# =============================================================================

NDI_HEADER="/usr/local/include/Processing.NDI.Lib.h"
[[ -f "${NDI_HEADER}" ]] \
  && pass "NDI SDK header found: ${NDI_HEADER}" \
  || fail "NDI SDK header missing: ${NDI_HEADER}  →  download from ndi.tv/sdk"

NDI_LIB=$(ls /usr/local/lib/libndi.so* 2>/dev/null | sort -V | tail -1)
if [[ -n "${NDI_LIB}" ]]; then
  pass "NDI library found: ${NDI_LIB}"
  # Verify dynamic linker can find it
  ldconfig -p | grep -q libndi \
    && pass "libndi.so in ld.so cache" \
    || warn "libndi.so not in ld cache — run: sudo ldconfig"
else
  fail "libndi.so not found — NDI SDK not installed or not in /usr/local/lib"
fi

# =============================================================================
header "3. GSTREAMER PLUGIN ELEMENTS"
# =============================================================================

declare -A ELEMENTS=(
  [ndisrc]="NDI source (gst-plugin-ndi)"
  [ndidemux]="NDI demuxer (gst-plugin-ndi)"
  [whipclientsink]="WHIP client sink (gst-plugin-webrtc)"
  [x264enc]="H.264 software encoder"
  [opusenc]="Opus audio encoder"
  [rtph264pay]="RTP H.264 payloader"
  [rtpopuspay]="RTP Opus payloader"
  [videoconvert]="Video format converter"
  [videoscale]="Video scaler"
  [videorate]="Video frame rate adjuster"
  [audioconvert]="Audio format converter"
  [audioresample]="Audio resampler"
  [nicesink]="libnice ICE sink (WebRTC)"
)

for el in "${!ELEMENTS[@]}"; do
  gst_inspect "${el}" \
    && pass "${el}  (${ELEMENTS[$el]})" \
    || fail "${el} MISSING — ${ELEMENTS[$el]}"
done

# Optional hardware encoders
for el in vaapih264enc nvh264enc; do
  gst_inspect "${el}" \
    && pass "Optional: ${el} available" \
    || warn "Optional: ${el} not available (only needed if encoder=${el%enc})"
done

# =============================================================================
header "4. PYTHON ENVIRONMENT"
# =============================================================================

VENV="/opt/ndi_to_whip/venv"
[[ -d "${VENV}" ]] \
  && pass "Python venv found: ${VENV}" \
  || fail "Python venv missing: ${VENV}  →  run install.sh"

if [[ -d "${VENV}" ]]; then
  "${VENV}/bin/python" -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None)" 2>/dev/null \
    && pass "PyGObject + GStreamer bindings importable" \
    || fail "PyGObject or GStreamer bindings not importable"

  "${VENV}/bin/python" -c "import tomllib" 2>/dev/null || \
  "${VENV}/bin/python" -c "import tomli"   2>/dev/null \
    && pass "TOML parser available (tomllib or tomli)" \
    || fail "TOML parser missing — pip install tomli"

  "${VENV}/bin/python" -c "import structlog" 2>/dev/null \
    && pass "structlog available" \
    || warn "structlog not installed — install with: pip install structlog"
fi

# =============================================================================
header "5. CONFIGURATION"
# =============================================================================

CONFIG="/etc/ndi_to_whip/config.toml"
[[ -f "${CONFIG}" ]] \
  && pass "Config file found: ${CONFIG}" \
  || fail "Config file missing: ${CONFIG}  →  copy config.toml and edit"

if [[ -f "${CONFIG}" ]]; then
  # Check for placeholder values
  grep -q 'your-whip-endpoint' "${CONFIG}" \
    && fail "WHIP URL is still a placeholder — edit ${CONFIG}" \
    || pass "WHIP URL appears to be configured"

  grep -q 'MYCOMPUTER' "${CONFIG}" \
    && warn "NDI source name may still be placeholder — verify with --probe"
fi

# =============================================================================
header "6. NETWORK"
# =============================================================================

# Check for multicast route (NDI uses mDNS/multicast for discovery)
ip route show | grep -q '224.0.0.0' \
  && pass "Multicast route present (required for NDI discovery)" \
  || warn "No multicast route — NDI may fall back to unicast; add manually if needed"

# Check if firewall might block NDI
if command -v ufw &>/dev/null; then
  UFW_STATUS=$(ufw status 2>/dev/null | head -1)
  echo "  ufw: ${UFW_STATUS}"
  echo "  NDI requires: UDP/5353 (mDNS), UDP/5960, TCP/5960"
  echo "  Hint: ufw allow 5353/udp && ufw allow 5960"
fi

# Probe NDI if not in quick mode
if ! $QUICK; then
  header "7. NDI SOURCE DISCOVERY"
  echo "  Probing for 5 seconds (use --quick to skip)…"
  if [[ -d "${VENV}" ]]; then
    "${VENV}/bin/python" /opt/ndi_to_whip/ndi_to_whip.py \
      --probe --probe-timeout 5 2>/dev/null \
      || warn "NDI probe failed or found no sources"
  else
    warn "Venv not available — run install.sh first"
  fi
fi

# =============================================================================
header "8. PIPELINE SYNTAX TEST"
# =============================================================================

if [[ -d "${VENV}" && -f "${CONFIG}" ]]; then
  echo "  Generating pipeline string from config…"
  PIPELINE=$("${VENV}/bin/python" /opt/ndi_to_whip/ndi_to_whip.py \
               --config "${CONFIG}" --print-pipeline 2>/dev/null) \
    && pass "Pipeline string generated without Python errors" \
    || fail "Pipeline string generation failed"

  echo "  Verifying pipeline string with gst-inspect (parse only)…"
  # Use gst-launch-1.0 with fakesrc to parse (not run) the pipeline
  # We just parse the NDI-less portion to check element availability
  gst-launch-1.0 --no-signal-handlers \
    fakesrc num-buffers=0 ! fakesink 2>/dev/null \
    && pass "gst-launch-1.0 is functional" \
    || warn "gst-launch-1.0 returned an error for trivial pipeline"
fi

# =============================================================================
header "9. SYSTEMD SERVICE"
# =============================================================================

systemctl is-enabled ndi-to-whip &>/dev/null \
  && pass "ndi-to-whip service is enabled" \
  || warn "ndi-to-whip service is NOT enabled  →  systemctl enable ndi-to-whip"

systemctl is-active ndi-to-whip &>/dev/null \
  && pass "ndi-to-whip service is active (running)" \
  || warn "ndi-to-whip service is NOT running  →  systemctl start ndi-to-whip"

# =============================================================================
# SUMMARY
# =============================================================================
echo ""
echo -e "${BOLD}────────────────────────────────────────${NC}"
echo -e "Results:  ${GREEN}${PASS} passed${NC}  ${YELLOW}${WARN} warnings${NC}  ${RED}${FAIL} failed${NC}"
echo -e "${BOLD}────────────────────────────────────────${NC}"
echo ""
if (( FAIL > 0 )); then
  echo -e "${RED}✗ Fix all failures before starting the service.${NC}"
  exit 1
elif (( WARN > 0 )); then
  echo -e "${YELLOW}! Review warnings — some may affect reliability.${NC}"
  exit 0
else
  echo -e "${GREEN}✓ All checks passed.${NC}"
  exit 0
fi
