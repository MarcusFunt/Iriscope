#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PACKAGE_LIST_SRC="${SCRIPT_DIR}/apt-packages.txt"
BOOT_UPDATE_SCRIPT_SRC="${SCRIPT_DIR}/iriscope-boot-update.sh"
BOOT_UPDATE_SERVICE_SRC="${SCRIPT_DIR}/iriscope-boot-update.service"
PACKAGE_LIST_DEST="/usr/local/share/iriscope/apt-packages.txt"
BOOT_UPDATE_BIN="/usr/local/bin/iriscope-boot-update"
BOOT_UPDATE_ENV="/etc/default/iriscope-boot-update"
BOOT_UPDATE_SERVICE="/etc/systemd/system/iriscope-boot-update.service"

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
REPO_URL="https://github.com/MarcusFunt/Iriscope.git"
REPO_BRANCH="main"
APP_ROOT="/opt/iriscope/app"

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
    --repo-url)
      REPO_URL="${2:-}"
      if [[ -z "${REPO_URL}" ]]; then
        echo "--repo-url requires a Git repository URL." >&2
        exit 1
      fi
      shift 2
      ;;
    --branch)
      REPO_BRANCH="${2:-}"
      if [[ -z "${REPO_BRANCH}" ]]; then
        echo "--branch requires a branch name." >&2
        exit 1
      fi
      shift 2
      ;;
    --app-root)
      APP_ROOT="${2:-}"
      if [[ -z "${APP_ROOT}" ]]; then
        echo "--app-root requires an absolute path." >&2
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

if [[ "${APP_ROOT}" != /* || "${APP_ROOT}" == "/" ]]; then
  echo "--app-root must be an absolute path other than /." >&2
  exit 1
fi

for source_file in "${PACKAGE_LIST_SRC}" "${BOOT_UPDATE_SCRIPT_SRC}" "${BOOT_UPDATE_SERVICE_SRC}"; do
  if [[ ! -f "${source_file}" ]]; then
    echo "Missing ${source_file}. Copy the full provision/pi-zero-w directory to the Pi before running setup." >&2
    exit 1
  fi
done

mapfile -t APT_PACKAGES < <(sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d' "${PACKAGE_LIST_SRC}")
if [[ "${#APT_PACKAGES[@]}" -eq 0 ]]; then
  echo "No packages found in ${PACKAGE_LIST_SRC}." >&2
  exit 1
fi

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

echo "Installing Iriscope Pi packages from ${PACKAGE_LIST_SRC}..."
apt-get install -y "${APT_PACKAGES[@]}"

echo "Enabling SSH and mDNS..."
systemctl enable --now ssh
systemctl enable --now avahi-daemon

echo "Creating Iriscope capture root at ${CAPTURE_ROOT}..."
install -d -m 0750 -o "${TARGET_USER}" -g "${TARGET_USER}" "${CAPTURE_ROOT}"
install -d -m 0750 -o "${TARGET_USER}" -g "${TARGET_USER}" "${CAPTURE_ROOT}/calibration"

echo "Installing Iriscope boot updater..."
install -d -m 0755 "$(dirname "${PACKAGE_LIST_DEST}")"
install -m 0644 "${PACKAGE_LIST_SRC}" "${PACKAGE_LIST_DEST}"
install -m 0755 "${BOOT_UPDATE_SCRIPT_SRC}" "${BOOT_UPDATE_BIN}"
install -m 0644 "${BOOT_UPDATE_SERVICE_SRC}" "${BOOT_UPDATE_SERVICE}"

{
  printf 'IRISCOPE_REPO_URL=%q\n' "${REPO_URL}"
  printf 'IRISCOPE_BRANCH=%q\n' "${REPO_BRANCH}"
  printf 'IRISCOPE_APP_ROOT=%q\n' "${APP_ROOT}"
  printf 'IRISCOPE_TARGET_USER=%q\n' "${TARGET_USER}"
} > "${BOOT_UPDATE_ENV}"
chmod 0644 "${BOOT_UPDATE_ENV}"

systemctl daemon-reload
systemctl enable iriscope-boot-update.service

echo "Running initial Iriscope repository sync..."
if ! systemctl start iriscope-boot-update.service; then
  echo "Warning: initial Iriscope repository sync failed; the service will retry on the next boot." >&2
fi

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
echo "Boot update logs: journalctl -u iriscope-boot-update.service"
