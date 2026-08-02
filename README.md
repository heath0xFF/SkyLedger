# SkyLedger

SkyLedger is a local ADS-B flight command center for a Raspberry Pi 4, NooElec NESDR SMArt XTR SDR, and 1090 MHz antenna. It reads local `readsb`/`tar1090` aircraft JSON, runs a FastAPI backend, shows a full-screen outdoor-TV dashboard, logs flyovers to SQLite, and can send Discord webhook alerts.

Core behavior does not require paid APIs or cloud services. Optional enrichment is intentionally modular and off by default.

## What It Includes

- FastAPI backend with WebSocket live updates.
- SQLite schema for aircraft, flyover events, daily stats, enrichment cache, and raw positions.
- Configurable ADS-B source path or URL.
- Distance calculation from your configured home location.
- Active flyover tracking with alert cooldowns.
- Altitude guessing countdown and reveal mode.
- Discord webhook alerts when enabled.
- TV-friendly dashboard, history page, aircraft page, and settings/status page.
- Raspberry Pi install script, systemd unit, and kiosk instructions.

## Quick Start On Raspberry Pi

For a plain-language walkthrough meant for first-time setup, open
[`docs/pi-setup-guide.html`](docs/pi-setup-guide.html) in a browser.

For a full technical zero-to-kiosk Raspberry Pi 4 setup, including OS install, `readsb`,
healthchecks, kiosk mode, and GitHub auto-updates, see
[`docs/raspberry-pi-setup.md`](docs/raspberry-pi-setup.md).

Install `readsb` and `tar1090` first, then verify one of these data sources exists:

```bash
ls /run/readsb/aircraft.json
ls /var/run/readsb/aircraft.json
ls /run/dump1090-fa/aircraft.json
curl http://localhost/tar1090/data/aircraft.json
```

Install SkyLedger:

```bash
cd /path/to/SkyLedger
chmod +x install.sh
./install.sh
```

Edit configuration:

```bash
sudo nano /etc/skyledger/config.yaml
```

Set at least:

```yaml
home_lat: <your_latitude>
home_lon: <your_longitude>
home_name: "Home Base"
adsb_json_path: "http://127.0.0.1/tar1090/data/aircraft.json"
```

Start manually:

```bash
cd /opt/skyledger
SKYLEDGER_CONFIG=/etc/skyledger/config.yaml ./venv/bin/python -m skyledger
```

Open [http://localhost:8000](http://localhost:8000).

## SDR Hardware Note

The NooElec NESDR SMArt XTR is still an RTL2832U RTL-SDR device, so SkyLedger does not need a different backend or dashboard configuration. `readsb`/`dump1090` should see it as an RTL-SDR receiver when the normal RTL-SDR drivers are installed.

The important caveat is ADS-B sensitivity. The SMArt XTR uses an E4000 tuner and is designed for extended tuning range. NooElec documents a small frequency gap near 1100 MHz and does not recommend the XTR model for ADS-B use. ADS-B is 1090 MHz, so real-world reception may be weak, inconsistent, or device-dependent.

Recommended path:

- Try the NESDR SMArt XTR first since you already have it.
- If aircraft count/range is poor even with a good 1090 MHz antenna placement, switch to an R820T/R820T2-based dongle such as a standard NooElec NESDR SMArt, FlightAware Pro Stick, or RTL-SDR Blog unit for ADS-B.
- Keep SkyLedger unchanged either way. The app reads `aircraft.json`; the SDR choice only affects how much ADS-B data `readsb` can decode.

## Install As A Service

The provided service assumes the Raspberry Pi user is `pi` and the app lives in `/opt/skyledger`.

```bash
cd /path/to/SkyLedger
INSTALL_SERVICE=1 ./install.sh
sudo systemctl start skyledger
sudo systemctl status skyledger
```

Logs:

```bash
journalctl -u skyledger -f
```

If your Pi username is not `pi`, edit `/etc/systemd/system/skyledger.service` and change `User=` and `Group=`.

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.local.yaml
python -m skyledger --config config.local.yaml
```

For a local test countdown, open [http://localhost:8000/settings](http://localhost:8000/settings) and press `Test Countdown`.

## Windows SDR Development

For Windows testing, SkyLedger uses Gvanem Dump1090 as a local decoder. The SDR must use the WinUSB driver first. In Device Manager, the NooElec dongle should show `SMArt XTR v5` with status `OK`.

Start the receiver in one PowerShell window:

```powershell
Copy-Item config.windows.yaml config.windows.local.yaml
.\scripts\start-windows-receiver.ps1
```

The first run clones [Gvanem Dump1090](https://github.com/gvanem/Dump1090) into `%LOCALAPPDATA%\SkyLedger\Dump1090`, downloads/builds its aircraft database, then starts decoding from the RTL-SDR. Leave this process running.

Confirm the decoder is serving JSON:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/data/aircraft.json
```

Start SkyLedger in a second PowerShell window:

```powershell
.\scripts\start-windows-skyledger.ps1
```

This uses `config.windows.local.yaml` when present, falling back to `config.windows.yaml`. The Windows config points `adsb_json_path` at `http://127.0.0.1:8080/data/aircraft.json` and writes to `skyledger.windows.db`. Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

If Dump1090 shows `messages` increasing but `aircraft` is empty, the SDR is working but no complete position has been decoded yet. Give it more time, improve antenna placement, or adjust gain. The XTR model may be weak for ADS-B at 1090 MHz.

## Configuration

Use `config.example.yaml` as the reference and keep real local values in `config.local.yaml`, `/etc/skyledger/config.yaml`, or another file passed with `--config`. Important fields:

- `home_lat`, `home_lon`: Your antenna/home position.
- `adsb_json_path`: File path or HTTP URL for `aircraft.json`.
- `alert_radius_miles`: Discord alert radius.
- `guess_trigger_radius_miles`: Countdown trigger radius.
- `tracking_radius_miles`: Live monitoring radius. Set to `0` to monitor every aircraft your antenna receives.
- `max_alert_altitude_ft`: Ignore high aircraft for alerts/countdowns.
- `countdown_seconds`: Guess countdown duration.
- `reveal_duration_seconds`: Reveal screen duration.
- `raw_position_retention_days`: Retention for raw position samples.
- `live_aircraft_timeout_seconds`: Hide aircraft whose latest message or position is older than this many seconds. Set to `0` to disable stale filtering.
- `enable_discord_alerts`, `discord_webhook_url`: Optional Discord notifications.
- `enable_enrichment`: Look up airline routes by callsign through ADSBDB and cache results locally.
- `map_zoom_level`: Dashboard map zoom, also adjustable from `/settings`.
- `map_tile_url`: Slippy-map tile URL template. Defaults to OpenStreetMap tiles and can point at a local tile server.

## Kiosk Mode

On Raspberry Pi OS Desktop, disable screen blanking:

```bash
xset s off
xset -dpms
xset s noblank
```

Launch Chromium:

```bash
chromium-browser --kiosk --noerrdialogs --disable-infobars http://localhost:8000
```

To auto-start on boot, create:

```bash
mkdir -p ~/.config/lxsession/LXDE-pi
nano ~/.config/lxsession/LXDE-pi/autostart
```

Add:

```text
@xset s off
@xset -dpms
@xset s noblank
@chromium-browser --kiosk --noerrdialogs --disable-infobars http://localhost:8000
```

## Dashboard Routes

- `/`: Outdoor TV dashboard.
- `/history`: Flyover history.
- `/aircraft/{hex}`: Aircraft history.
- `/settings`: Status, config, and test controls.

API:

- `GET /api/status`
- `GET /api/aircraft/live`
- `GET /api/events/recent`
- `GET /api/stats/today`
- `GET /api/aircraft/{hex}`
- `GET /api/history`
- `POST /api/test-discord`
- `POST /api/test-countdown`
- `WS /ws/live`

Administrative `POST` endpoints are available without credentials only from
localhost. For remote administration, set `SKYLEDGER_ADMIN_TOKEN` in the service
environment and send `Authorization: Bearer <token>`. Read-only dashboard and
history routes remain available on the configured bind address.

## Database Maintenance

Create a transactionally consistent backup while SkyLedger is running:

```bash
python -m skyledger.maintenance --config /etc/skyledger/config.yaml backup /path/to/backups/skyledger.db
```

The command uses SQLite's online backup API, includes committed WAL data, and
checks the backup before replacing the destination. Check the live database with:

```bash
python -m skyledger.maintenance --config /etc/skyledger/config.yaml check
```

To restore, stop SkyLedger, preserve the damaged database and its `-wal`/`-shm`
files, copy a verified backup to `database_path`, run the `check` command, and
then restart SkyLedger. Keep backups on storage separate from the receiver.

## License

SkyLedger is available under the custom SkyLedger Non-Commercial License. It is free to use, modify, and share for non-commercial purposes. Commercial use requires a separate written commercial license from the copyright holder.

## Notes

SkyLedger treats local ADS-B data as the source of truth. If the ADS-B JSON file is missing, malformed, or empty, the dashboard stays up and reports receiver status instead of crashing.
