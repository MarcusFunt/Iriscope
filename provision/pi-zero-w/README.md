# Raspberry Pi Zero W SD Card Preparation

## Recommended Image

Flash **Raspberry Pi OS (Legacy) Lite, 32-bit**.

As of June 16, 2026, Raspberry Pi lists the normal 32-bit Lite image as Debian 13 Trixie, and the Legacy 32-bit Lite image as Debian 12 Bookworm with security updates. The original Pi Zero W is supported by the Legacy 32-bit image, and Bookworm is the lower-risk camera stack for this capture appliance.

Do not flash:

- 64-bit Raspberry Pi OS: the original Pi Zero W is not a 64-bit board.
- Ubuntu Server: Pi Zero W is ARMv6, and Ubuntu support is poor for this use.
- Desktop/Full images: the Zero W has limited CPU/RAM and Iriscope captures headlessly.

Use a 16 GB or larger microSD card from a reliable brand. 32 GB is a practical minimum if you plan to leave several DNG capture sessions on the Pi.

## Imager Settings

Use Raspberry Pi Imager and choose:

- Device: Raspberry Pi Zero / Zero W
- OS: Raspberry Pi OS (Legacy) Lite, 32-bit
- Hostname: `iriscope-pi`
- Enable SSH: yes
- Username/password: set your own user; avoid the old default `pi` if possible
- Wi-Fi: configure your 2.4 GHz network
- Locale/timezone: your local settings

After first boot, find the Pi:

```bash
ssh <user>@iriscope-pi.local
```

## Provision The Pi

Copy the full Pi provisioning directory to the Pi:

```powershell
scp -r provision\pi-zero-w <user>@iriscope-pi.local:/tmp/iriscope-pi
```

Run it on the Pi:

```bash
chmod +x /tmp/iriscope-pi/setup-iriscope-pi.sh
sudo /tmp/iriscope-pi/setup-iriscope-pi.sh
```

To use a wired preview/control link instead of Wi-Fi, enable USB Ethernet gadget mode during provisioning:

```bash
sudo /tmp/iriscope-pi/setup-iriscope-pi.sh --enable-usb-ethernet --usb-ip 10.42.0.2
```

To point the boot updater at a different checkout source, override the defaults:

```bash
sudo /tmp/iriscope-pi/setup-iriscope-pi.sh \
  --repo-url https://github.com/MarcusFunt/Iriscope.git \
  --branch main \
  --app-root /opt/iriscope/app \
  --network-wait-s 120
```

After reboot, connect the computer to the Pi Zero **USB/data** port, not the PWR-only port. Configure the computer-side USB/RNDIS interface with an address on the same subnet, for example `10.42.0.1/24`, then use `10.42.0.2` as the Iriscope Pi host.

Reboot:

```bash
sudo reboot
```

## Boot Auto-Update

Provisioning installs `/usr/local/bin/iriscope-boot-update` and enables `iriscope-boot-update.service`. On every boot, after `network-online.target`, the service:

- fetches `origin/main` from `https://github.com/MarcusFunt/Iriscope.git`
- resets and cleans `/opt/iriscope/app` so local edits there are discarded
- installs the Pi capture package list from `provision/pi-zero-w/apt-packages.txt`

The updater is best effort. If GitHub or apt is unavailable, it logs the failure and keeps the existing Pi install usable. Capture files stay outside the resettable checkout under `/home/<user>/iriscope`.

Run it manually:

```bash
sudo systemctl start iriscope-boot-update.service
journalctl -u iriscope-boot-update.service
```

The service configuration lives in `/etc/default/iriscope-boot-update`:

```bash
IRISCOPE_REPO_URL=https://github.com/MarcusFunt/Iriscope.git
IRISCOPE_BRANCH=main
IRISCOPE_APP_ROOT=/opt/iriscope/app
IRISCOPE_TARGET_USER=<user>
IRISCOPE_NETWORK_WAIT_S=120
```

The Pi Zero W is kept to the capture stack only. Python processing extras, the WebRTC/API stack, and npm web dependencies are installed on the computer-side development machine, not on the Pi.

## Verify Camera Capture

After reboot:

```bash
ssh <user>@iriscope-pi.local
rpicam-hello --list-cameras
iriscope-camera-smoke-test
```

The smoke test writes DNG/JPEG/JSON files into `/home/<user>/iriscope/smoke-test/`.

If `rpicam-hello --list-cameras` shows no IMX477/HQ camera:

1. Power off the Pi.
2. Reseat the camera ribbon cable and confirm it is the smaller Pi Zero camera cable.
3. Boot again and retry.
4. If it still fails, edit `/boot/firmware/config.txt` or `/boot/config.txt` and add `dtoverlay=imx477`, then reboot.

## Computer Config

Set `.iriscope.toml` on your computer to match the Pi:

```toml
[pi]
host = "iriscope-pi.local"
user = "<user>"
remote_root = "/home/<user>/iriscope"
```

For USB Ethernet gadget mode, use the static USB-side address instead:

```toml
[pi]
host = "10.42.0.2"
user = "<user>"
remote_root = "/home/<user>/iriscope"
```

Then run from this repository:

```powershell
python -m iriscope calibrate
python -m iriscope capture --subject S001 --eye left --count 12
```
