# Raspberry Pi 4 SkyLedger Zero-to-Kiosk Guide

This guide takes a Raspberry Pi 4 Model B and a 32 GB microSD card from blank hardware to a self-starting SkyLedger kiosk. It assumes the Pi will eventually run without a mouse or keyboard, but with a display connected for the kiosk dashboard.

Target result:

- Raspberry Pi OS Desktop boots automatically.
- `readsb` starts the ADS-B receiver and writes `/run/readsb/aircraft.json`.
- SkyLedger starts as a systemd service at `http://skyledger-pi.local:8000`.
- Chromium opens SkyLedger in kiosk mode after desktop login.
- A healthcheck timer restarts `readsb` or SkyLedger when the receiver path goes stale.
- A Git update service pulls fast-forward-only updates from GitHub at boot and on a timer.

## 1. Flash Raspberry Pi OS

Use Raspberry Pi Imager on your normal computer.

1. Insert the 32 GB microSD card.
2. Choose device: `Raspberry Pi 4`.
3. Choose OS: `Raspberry Pi OS (64-bit)` with Desktop. Do not choose Lite for this setup because the kiosk needs Chromium and a desktop session.
4. Choose storage: your microSD card.
5. Open OS customization and set:
   - Hostname: `skyledger-pi`
   - Username: `pi`
   - Password: a strong password
   - Wi-Fi SSID/password, unless you will use Ethernet
   - Locale/timezone: your actual locale and timezone
   - SSH: enabled
6. Write the card, eject it, insert it into the Pi, connect HDMI, connect the SDR and antenna, then power on.

The repo service files assume the Linux username is `pi`. You can use a different username, but then edit `User=`, `Group=`, and `/home/pi/SkyLedger` in the systemd units.

## 2. First Login And Base Packages

From your computer:

```bash
ssh pi@skyledger-pi.local
```

If `.local` does not resolve, find the Pi IP address in your router and use:

```bash
ssh pi@<pi-ip-address>
```

Update the OS and install the packages SkyLedger needs:

```bash
sudo apt update
sudo apt -y full-upgrade
sudo apt install -y git python3-venv python3-pip rsync curl wget
command -v chromium || command -v chromium-browser || sudo apt install -y chromium-browser
sudo raspi-config nonint do_blanking 1
sudo reboot
```

After reboot:

```bash
ssh pi@skyledger-pi.local
```

## 3. Install ADS-B Receiver Software

SkyLedger reads decoded ADS-B JSON. It does not talk to the SDR directly on the Pi. Use `readsb` for decoding and `tar1090` for the local receiver map.

Install `readsb`:

```bash
sudo bash -c "$(wget -O - https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)"
sudo reboot
```

Reconnect after reboot and set your receiver location:

```bash
sudo readsb-set-location <your_latitude> <your_longitude>
sudo readsb-gain auto
```

Install `tar1090`:

```bash
sudo bash -c "$(wget -nv -O - https://github.com/wiedehopf/tar1090/raw/master/install.sh)"
```

Verify receiver output:

```bash
systemctl status readsb --no-pager
ls -l /run/readsb/aircraft.json
curl http://127.0.0.1/tar1090/data/aircraft.json
```

Open the local receiver map from another computer:

```text
http://skyledger-pi.local/tar1090
```

If `readsb` cannot use the SDR, check whether the kernel DVB driver claimed the dongle:

```bash
sudo journalctl --no-pager -u readsb
```

If the log mentions the kernel driver or a claimed RTL-SDR device, run:

```bash
echo -e 'blacklist rtl2832\nblacklist dvb_usb_rtl28xxu\nblacklist rtl8192cu\nblacklist rtl8xxxu\n' | sudo tee /etc/modprobe.d/blacklist-rtl-sdr.conf
sudo reboot
```

## 4. Clone SkyLedger

Clone the GitHub repo into the Pi user's home directory. This clone is the source checkout used by the auto-update service.

```bash
cd ~
git clone https://github.com/cjs007/SkyLedger.git
cd ~/SkyLedger
git checkout main
chmod +x install.sh scripts/pi-healthcheck.sh scripts/skyledger-update.sh
```

If the repo is private, use an SSH deploy key instead of HTTPS:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/skyledger_deploy -C "skyledger-pi"
cat ~/.ssh/skyledger_deploy.pub
```

Add that public key in GitHub as a read-only deploy key, then configure SSH:

```bash
cat > ~/.ssh/config <<'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/skyledger_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
git remote set-url origin git@github.com:cjs007/SkyLedger.git
ssh -T git@github.com
```

## 5. Install SkyLedger Runtime And Services

Run the installer from the source checkout:

```bash
cd ~/SkyLedger
INSTALL_SERVICE=1 INSTALL_HEALTHCHECK=1 INSTALL_UPDATE_SERVICE=1 ./install.sh
```

Edit the Pi-specific config:

```bash
sudo nano /etc/skyledger/config.yaml
```

Set at least these values:

```yaml
home_lat: <your_latitude>
home_lon: <your_longitude>
home_name: "Home Base"
dashboard_title: "SkyLedger"
adsb_json_path: "http://127.0.0.1/tar1090/data/aircraft.json"
database_path: "skyledger.db"
dashboard_port: 8000
tar1090_url: "http://localhost/tar1090/"
tracking_radius_miles: 0.0
```

The tar1090 JSON endpoint is preferred here because it matches the receiver map you verify in the browser. If you intentionally want to read the file directly, use the populated readsb JSON path from your Pi instead.

Allow the SkyLedger service user to save settings from the web UI:

```bash
sudo chown pi:pi /etc/skyledger /etc/skyledger/config.yaml
sudo chmod 750 /etc/skyledger
sudo chmod 640 /etc/skyledger/config.yaml
```

Start SkyLedger:

```bash
sudo systemctl start skyledger
sudo systemctl status skyledger --no-pager
curl http://127.0.0.1:8000/api/status
```

From another computer, open:

```text
http://skyledger-pi.local:8000
```

## 6. Healthcheck And Self-Healing Behavior

The installed healthcheck timer runs every minute after boot. It checks:

- `skyledger.service` is active.
- `http://127.0.0.1:8000/api/status` responds.
- `readsb.service` is active.
- The ADS-B source from SkyLedger status exists and is fresh.
- `receiver_online` is true after SkyLedger can read the source.

If the decoder JSON file is missing or stale, it restarts `readsb.service`. If the SkyLedger API is down, it restarts `skyledger.service`.

Inspect it:

```bash
systemctl list-timers 'skyledger*'
systemctl status skyledger-healthcheck.timer --no-pager
journalctl -u skyledger-healthcheck.service -n 100 --no-pager
```

Run a receiver recovery drill:

```bash
sudo systemctl stop readsb
sleep 90
systemctl is-active readsb
curl http://127.0.0.1:8000/api/status
```

`readsb` should come back automatically. SkyLedger may briefly show `receiver_online=false`, then flip back once the configured ADS-B JSON source is readable again.

## 7. GitHub Auto-Update Workflow

The update service uses:

- Source checkout: `/home/pi/SkyLedger`
- Remote: `origin`
- Branch: `main`
- Runtime copy: `/opt/skyledger`
- Config: `/etc/skyledger/config.yaml`

At boot, `skyledger-update.service` runs before `skyledger.service`. The timer also checks every 15 minutes. It does:

1. `git fetch --prune origin`
2. `git checkout main` if needed
3. `git merge --ff-only origin/main`
4. `./install.sh`
5. `systemctl try-restart skyledger.service`

It intentionally does not overwrite local commits or local file edits. If the Pi checkout is dirty or diverged, the update fails and leaves the current app running.

Manual update command:

```bash
sudo systemctl start skyledger-update.service
journalctl -u skyledger-update.service -n 100 --no-pager
```

Normal development flow:

```bash
# on your main computer
git add .
git commit -m "Update SkyLedger dashboard"
git push origin main

# on the Pi, either wait for the timer or run:
sudo systemctl start skyledger-update.service
```

If you need to change the deploy branch:

```bash
sudo systemctl edit skyledger-update.service
```

Add:

```ini
[Service]
Environment=SKYLEDGER_BRANCH=main
```

Then reload and run:

```bash
sudo systemctl daemon-reload
sudo systemctl start skyledger-update.service
```

## 8. Kiosk Mode

Raspberry Pi OS Bookworm uses `labwc` autostart for desktop kiosk startup. Create the autostart file:

```bash
mkdir -p ~/.config/labwc
nano ~/.config/labwc/autostart
```

Add:

```bash
sh -c 'chromium http://127.0.0.1:8000 --kiosk --noerrdialogs --disable-infobars --no-first-run --enable-features=OverlayScrollbar --start-maximized || chromium-browser http://127.0.0.1:8000 --kiosk --noerrdialogs --disable-infobars --no-first-run --enable-features=OverlayScrollbar --start-maximized' &
```

Make sure the desktop auto-login is enabled:

```bash
sudo raspi-config
```

Choose `System Options` -> `Boot / Auto Login` -> `Desktop Autologin`.

Reboot:

```bash
sudo reboot
```

The Pi should boot, update SkyLedger if GitHub has a new fast-forward commit, start the ADS-B receiver and SkyLedger service, log into the desktop, and open Chromium full-screen to the local dashboard.

## 9. Useful Operations

View service status:

```bash
systemctl status readsb tar1090 skyledger --no-pager
systemctl list-timers 'skyledger*'
```

Follow logs:

```bash
journalctl -u readsb -f
journalctl -u skyledger -f
journalctl -u skyledger-healthcheck.service -f
journalctl -u skyledger-update.service -f
```

Restart services manually:

```bash
sudo systemctl restart readsb
sudo systemctl restart skyledger
```

Confirm the whole path:

```bash
curl http://127.0.0.1/tar1090/data/aircraft.json
curl http://127.0.0.1:8000/api/status
```

## 10. Troubleshooting

`receiver_online=false`:

1. Check `readsb`: `systemctl status readsb --no-pager`
2. Check raw JSON: `curl http://127.0.0.1/tar1090/data/aircraft.json`
3. Check SkyLedger status: `curl http://127.0.0.1:8000/api/status`
4. Check logs: `journalctl -u readsb -n 100 --no-pager`

Kiosk opens before SkyLedger is ready:

- Wait a few seconds and refresh with `Ctrl+R`.
- Check `systemctl status skyledger --no-pager`.
- Chromium points at `127.0.0.1`, so it will work even if Wi-Fi is down after boot.

GitHub updates do not land:

1. Run `sudo systemctl start skyledger-update.service`.
2. Read `journalctl -u skyledger-update.service -n 100 --no-pager`.
3. If the repo is dirty, clean or commit the local change in `/home/pi/SkyLedger`.
4. If SSH fails, verify the deploy key with `ssh -T git@github.com`.

Poor aircraft count or range:

- Put the antenna near a window or outdoors if possible.
- Verify `readsb` is running before debugging SkyLedger.
- The NooElec NESDR SMArt XTR may be inconsistent for 1090 MHz ADS-B. If range is poor after good antenna placement and gain tuning, try an R820T/R820T2-based ADS-B dongle.

## References

- Raspberry Pi getting started and Imager customization: https://www.raspberrypi.com/documentation/computers/getting-started.html
- Raspberry Pi kiosk mode with `labwc` autostart: https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/
- Raspberry Pi screen blanking configuration: https://www.raspberrypi.com/documentation/computers/configuration.html#configure-screen-blanking
- `readsb` automatic installation: https://github.com/wiedehopf/adsb-scripts/wiki/Automatic-installation-for-readsb
- `tar1090` installation: https://github.com/wiedehopf/tar1090
