#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z "${TARGET_USER}" || "${TARGET_USER}" == "root" ]]; then
  TARGET_USER="$(getent passwd 1000 | cut -d: -f1 || true)"
fi
if [[ -z "${TARGET_USER}" ]]; then
  echo "Could not determine the normal login user." >&2
  exit 1
fi

TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
CAPTURE_ROOT="${TARGET_HOME}/iriscope"
BOOT_CONFIG=""
BOOT_CMDLINE=""
ENABLE_USB_ETHERNET=0
USB_IP="10.42.0.2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable-usb-ethernet)
      ENABLE_USB_ETHERNET=1
      shift
      ;;
    --usb-ip)
      USB_IP="${2:-}"
      if [[ -z "${USB_IP}" ]]; then
        echo "--usb-ip requires an address." >&2
        exit 1
      fi
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -f /boot/firmware/config.txt ]]; then
  BOOT_CONFIG="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
  BOOT_CONFIG="/boot/config.txt"
fi
if [[ -f /boot/firmware/cmdline.txt ]]; then
  BOOT_CMDLINE="/boot/firmware/cmdline.txt"
elif [[ -f /boot/cmdline.txt ]]; then
  BOOT_CMDLINE="/boot/cmdline.txt"
fi

echo "Updating package lists..."
apt-get update

echo "Installing required capture and transfer packages..."
apt-get install -y \
  openssh-server \
  rsync \
  rpicam-apps

echo "Installing optional diagnostics and maintenance packages..."
apt-get install -y \
  avahi-daemon \
  curl \
  dnsutils \
  exiftool \
  git \
  i2c-tools \
  jq \
  less \
  tmux \
  v4l-utils \
  vim-tiny

echo "Enabling SSH and mDNS..."
systemctl enable --now ssh
systemctl enable --now avahi-daemon

echo "Creating Iriscope capture root at ${CAPTURE_ROOT}..."
install -d -m 0750 -o "${TARGET_USER}" -g "${TARGET_USER}" "${CAPTURE_ROOT}"
install -d -m 0750 -o "${TARGET_USER}" -g "${TARGET_USER}" "${CAPTURE_ROOT}/calibration"

if [[ -n "${BOOT_CONFIG}" ]]; then
  echo "Ensuring camera auto-detect is enabled in ${BOOT_CONFIG}..."
  if grep -qE '^[#[:space:]]*camera_auto_detect=' "${BOOT_CONFIG}"; then
    sed -i 's/^[#[:space:]]*camera_auto_detect=.*/camera_auto_detect=1/' "${BOOT_CONFIG}"
  else
    printf '\n# Iriscope camera capture\ncamera_auto_detect=1\n' >> "${BOOT_CONFIG}"
  fi
else
  echo "Warning: could not find boot config; skipping camera_auto_detect setting." >&2
fi

if [[ "${ENABLE_USB_ETHERNET}" -eq 1 ]]; then
  if [[ -z "${BOOT_CONFIG}" || -z "${BOOT_CMDLINE}" ]]; then
    echo "Could not find boot config/cmdline files for USB Ethernet gadget setup." >&2
    exit 1
  fi

  echo "Enabling Pi Zero USB Ethernet gadget mode..."
  if ! grep -qE '^[[:space:]]*dtoverlay=dwc2' "${BOOT_CONFIG}"; then
    printf '\n# Iriscope USB Ethernet gadget\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "${BOOT_CONFIG}"
  fi

  cmdline="$(<"${BOOT_CMDLINE}")"
  if [[ "${cmdline}" != *"g_ether"* ]]; then
    if [[ "${cmdline}" == *"rootwait"* ]]; then
      cmdline="${cmdline/rootwait/rootwait modules-load=dwc2,g_ether}"
    else
      cmdline="${cmdline} modules-load=dwc2,g_ether"
    fi
    printf '%s\n' "${cmdline}" > "${BOOT_CMDLINE}"
  fi

  if command -v nmcli >/dev/null 2>&1; then
    echo "Configuring static USB gadget address ${USB_IP}/24 on usb0..."
    if nmcli -t -f NAME connection show | grep -qx 'iriscope-usb0'; then
      nmcli connection modify iriscope-usb0 \
        connection.interface-name usb0 \
        ipv4.method manual \
        ipv4.addresses "${USB_IP}/24" \
        ipv4.never-default yes \
        ipv6.method disabled
    else
      nmcli connection add type ethernet ifname usb0 con-name iriscope-usb0 \
        ipv4.method manual \
        ipv4.addresses "${USB_IP}/24" \
        ipv4.never-default yes \
        ipv6.method disabled
    fi
  else
    echo "Warning: nmcli not found; configure usb0=${USB_IP}/24 manually after reboot." >&2
  fi
fi

echo "Installing iriscope-camera-smoke-test..."
cat >/usr/local/bin/iriscope-camera-smoke-test <<'SMOKETEST'
#!/usr/bin/env bash
set -euo pipefail

CAPTURE_ROOT="${HOME}/iriscope"
OUT_DIR="${CAPTURE_ROOT}/smoke-test"
mkdir -p "${OUT_DIR}"

echo "Camera list:"
rpicam-hello --list-cameras

echo "Capturing DNG/JPEG smoke-test frame..."
cd "${OUT_DIR}"
TUNING_ARGS=()
if [ -f /usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json ]; then
  TUNING_ARGS=(--tuning-file /usr/share/libcamera/ipa/rpi/vc4/imx477_scientific.json)
fi
rpicam-still --raw --immediate --nopreview \
  "${TUNING_ARGS[@]}" \
  --awb auto \
  --metering centre \
  --exposure normal \
  --denoise cdn_fast \
  --metadata smoke.json \
  --metadata-format json \
  --quality 95 \
  -o smoke.jpg

echo "Files written:"
ls -lh smoke.jpg smoke.dng smoke.json

if command -v exiftool >/dev/null 2>&1; then
  echo "DNG metadata summary:"
  exiftool -Model -ImageWidth -ImageHeight -ExposureTime -AnalogueGain smoke.dng || true
fi
SMOKETEST
chmod 0755 /usr/local/bin/iriscope-camera-smoke-test

cat >"${CAPTURE_ROOT}/README.txt" <<EOF
Iriscope capture root.

The computer-side Iriscope CLI writes remote sessions here over SSH:
  ${CAPTURE_ROOT}/<subject>_<eye>_<timestamp>/

Run this on the Pi to verify the HQ camera:
  iriscope-camera-smoke-test
EOF
chown "${TARGET_USER}:${TARGET_USER}" "${CAPTURE_ROOT}/README.txt"

echo
echo "Provisioning complete."
echo "Recommended next step: sudo reboot"
echo "After reboot, run: iriscope-camera-smoke-test"
