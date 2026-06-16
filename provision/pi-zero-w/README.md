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

Copy the setup script to the Pi:

```powershell
scp provision\pi-zero-w\setup-iriscope-pi.sh <user>@iriscope-pi.local:/tmp/setup-iriscope-pi.sh
```

Run it on the Pi:

```bash
chmod +x /tmp/setup-iriscope-pi.sh
sudo /tmp/setup-iriscope-pi.sh
```

Reboot:

```bash
sudo reboot
```

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

Then run from this repository:

```powershell
python -m iriscope calibrate
python -m iriscope capture --subject S001 --eye left --count 12
```
