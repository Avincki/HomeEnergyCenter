# Raspberry Pi 5 — first-time setup, install & kiosk display

This is a start-to-finish walkthrough for putting HomeEnergyCenter on a brand-new
Raspberry Pi 5: flashing the OS, installing the app, starting it automatically at
boot, and showing the dashboard full-screen ("frameless") on an attached monitor.

If you only want the headless server (no monitor) + phone access over Tailscale,
see `setup-guide.html` instead — this guide is the kiosk/wall-display variant.

> **Network requirement:** the Pi must be on the **same LAN** as your inverter,
> battery and meters (this project assumes the `192.168.129.0/24` subnet). A
> cloud host cannot reach Modbus / the local device APIs.

---

## 0. What you need

- Raspberry Pi 5 (4 GB is enough; 8 GB is fine too) + official 27 W USB-C PSU.
- A 32 GB+ A2-class microSD card (or, better for longevity, an SSD on USB-3).
- Ethernet cable to your home router (preferred over Wi-Fi for a 24/7 box).
- For the kiosk display: a monitor + micro-HDMI→HDMI cable, and a keyboard for
  first boot.
- Your `config.yaml` (or `config.example.yaml` as a starting point) with device
  IPs, the ENTSO-E API token, and tariff settings.
- Another computer to run **Raspberry Pi Imager** and to SSH in.

---

## 1. Flash Raspberry Pi OS

1. Install **Raspberry Pi Imager** from <https://www.raspberrypi.com/software/>
   on your laptop/PC and launch it.
2. **Choose Device:** Raspberry Pi 5.
3. **Choose OS:**
   - If you want the dashboard on an attached screen → **Raspberry Pi OS (64-bit)**
     — the *full desktop* edition. (It includes the Chromium browser and a
     graphical session, which we need for kiosk mode.)
   - If you only want the headless server → **Raspberry Pi OS Lite (64-bit)**.
4. **Choose Storage:** your microSD card / SSD.
5. Click **Next → Edit Settings** and pre-configure:
   - **Hostname:** `energycenter`
   - **Username / password:** pick your own (don't leave it as `pi` / `raspberry`).
   - **Wireless LAN:** only if you can't use Ethernet (Ethernet is preferred).
   - **Locale:** time zone `Europe/Brussels`, keyboard layout to taste.
   - **Services tab → Enable SSH** (use *password* or, better, *public-key*).
6. **Save → Write.** When it finishes, put the card in the Pi, connect Ethernet
   (and the monitor + keyboard if you're doing the kiosk), then power on.

First boot takes a minute or two while it resizes the filesystem and reboots.

---

## 2. First login & system update

From your laptop:

```bash
ssh youruser@energycenter.local
```

(If `.local` doesn't resolve, check your router for the Pi's IP and use that.)

Update everything and install the build prerequisites:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
    python3 python3-venv python3-pip python3-dev \
    git build-essential libssl-dev libffi-dev \
    sqlite3 curl ca-certificates
python3 --version          # expect 3.11.x or newer (Bookworm ships 3.11)
sudo reboot                # if the kernel/firmware was updated
```

While you're here, open `sudo raspi-config` and:
- **System Options → Hostname** — confirm `energycenter`.
- **Display Options → Screen Blanking → No** — so the wall display never sleeps.
- (Kiosk only) **System Options → Boot / Auto Login → Desktop Autologin** — boots
  straight into the graphical session without a login prompt.

---

## 3. Install HomeEnergyCenter

### 3.1 Create a service account

Keeping the app under its own user is tidier and a little safer:

```bash
sudo useradd --system --create-home --home-dir /opt/energycenter --shell /bin/bash energycenter
sudo -u energycenter -i        # you're now /opt/energycenter as that user
```

### 3.2 Get the code

Clone it (replace with your repo URL):

```bash
git clone <your-repo-url> HomeEnergyCenter
cd HomeEnergyCenter
```

Or copy it from your Windows PC with `rsync` (run from WSL / Git Bash on the PC),
excluding local junk:

```bash
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.db' \
  ./HomeEnergyCenter/ energycenter@energycenter.local:/opt/energycenter/HomeEnergyCenter/
```

### 3.3 Virtualenv + install

```bash
cd /opt/energycenter/HomeEnergyCenter
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -e .
```

On a Pi this is quick if prebuilt ARM64 wheels exist; if `pip` falls back to
building `pymodbus` / `aiohttp` from source, give it 10–15 minutes — it's a
one-off.

### 3.4 Configure

```bash
cp config.example.yaml config.yaml
nano config.yaml
```

At minimum set:
- **Device IPs** — sonnenBatterie, P1 meter, car charger, small/large solar,
  SolarEdge inverter (use the static IPs you reserved on the router).
- **`prices.api_key`** — your ENTSO-E token.
- **`web:`** — leave `host: 0.0.0.0`, `port: 8000` so the dashboard is reachable
  from the LAN (and from the Pi's own browser at `localhost:8000`).
- Keep **`decision.dry_run: true`** for the first weeks — it suppresses all
  actuator writes to the inverter while you watch it behave.

### 3.5 Initialise the database

```bash
alembic upgrade head
```

### 3.6 Smoke test

```bash
python main.py
```

You should see structured logs from the device pollers and uvicorn announcing
it's listening on `0.0.0.0:8000`. From any device on the LAN open
`http://energycenter.local:8000/` (or `http://<pi-ip>:8000/`) — the dashboard
should load. Hit `Ctrl+C` to stop, then `exit` to leave the `energycenter` user
shell.

---

## 4. Run it automatically at boot (systemd service)

A systemd unit gives you auto-start at boot, auto-restart on crash, and logs in
`journalctl`.

```bash
sudo nano /etc/systemd/system/energycenter.service
```

```ini
[Unit]
Description=HomeEnergyCenter (energy orchestrator)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=energycenter
Group=energycenter
WorkingDirectory=/opt/energycenter/HomeEnergyCenter
Environment=EO_CONFIG=/opt/energycenter/HomeEnergyCenter/config.yaml
ExecStart=/opt/energycenter/HomeEnergyCenter/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
# Light hardening — it only needs LAN access and its own data dir.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/energycenter/HomeEnergyCenter

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now energycenter.service
sudo systemctl status energycenter.service
journalctl -u energycenter.service -f      # live logs; Ctrl+C to stop watching
```

Handy later:

| Action | Command |
|---|---|
| Restart after a config change | `sudo systemctl restart energycenter` |
| Stop | `sudo systemctl stop energycenter` |
| Disable autostart | `sudo systemctl disable energycenter` |
| Last 200 log lines | `journalctl -u energycenter -n 200 --no-pager` |

The service is now independent of the kiosk display — even with no monitor
attached, the app runs.

---

## 5. Show the dashboard full-screen ("frameless")

This needs the **desktop** image (section 1) with **Desktop Autologin** enabled
(section 2). The idea: when the graphical session starts, launch Chromium in
kiosk mode pointed at `http://localhost:8000`. Kiosk mode = full screen, no tabs,
no address bar, no window borders.

Raspberry Pi OS *Bookworm* on a Pi 5 uses a Wayland session. Depending on your
image build the compositor is either **labwc** (newer) or **wayfire** (older) —
the steps below cover both; do the one that matches your Pi.

### 5.1 Disable screen blanking & hide the cursor

```bash
# Already done if you set raspi-config → Display Options → Screen Blanking → No.
sudo apt install -y unclutter            # auto-hides the mouse pointer
```

### 5.2a If your session is **labwc** (default on recent Pi 5 images)

```bash
mkdir -p ~/.config/labwc
nano ~/.config/labwc/autostart
```

```sh
unclutter --timeout 1 &
chromium-browser --kiosk --noerrdialogs --disable-infobars --no-first-run \
  --check-for-update-interval=31536000 \
  --app=http://localhost:8000 &
```

### 5.2b If your session is **wayfire**

```bash
nano ~/.config/wayfire.ini
```

Add (or extend) an `[autostart]` section:

```ini
[autostart]
unclutter = unclutter --timeout 1
chromium = chromium-browser --kiosk --noerrdialogs --disable-infobars --no-first-run --check-for-update-interval=31536000 --app=http://localhost:8000
screensaver = false
dpms = false
```

### 5.2c (Alternative) Lite image + `cage`

If you used the **Lite** image and want the smallest possible kiosk, run Chromium
under the `cage` compositor as a service instead of a full desktop:

```bash
sudo apt install -y cage chromium-browser
sudo loginctl enable-linger $USER
```

Create `/etc/systemd/system/kiosk.service`:

```ini
[Unit]
Description=Chromium kiosk
After=energycenter.service systemd-user-sessions.service
Wants=energycenter.service

[Service]
User=youruser
PAMName=login
TTYPath=/dev/tty1
Environment=XDG_RUNTIME_DIR=/run/user/%U
ExecStartPre=/bin/sh -c 'until curl -sf http://localhost:8000/ >/dev/null; do sleep 2; done'
ExecStart=/usr/bin/cage -- chromium-browser --kiosk --noerrdialogs --disable-infobars --no-first-run --app=http://localhost:8000
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now kiosk.service
```

### 5.3 Reboot and check

```bash
sudo reboot
```

After the reboot the monitor should come up straight into the dashboard, full
screen, no browser chrome. To get out: SSH in and `pkill chromium` (or
`sudo systemctl stop kiosk` for the `cage` setup), or press `Ctrl+Alt+F2` for a
text console (`Ctrl+Alt+F1`/`F7` to go back).

---

## 6. Quick troubleshooting

| Symptom | Check |
|---|---|
| `energycenter.local` doesn't resolve | Use the IP from your router; `ip -4 addr show` on the Pi to find it (look for `192.168.129.x`). |
| Dashboard loads but tiles say a source is unreachable | Open `http://<pi>:8000/debug` — the health panel shows, per device, whether it's configured and reachable on its port. Fix the IP/token in `config.yaml`, then `sudo systemctl restart energycenter`. |
| Service won't start | `journalctl -u energycenter -n 100 --no-pager` — usually a `config.yaml` validation error or a wrong path in the unit file. |
| Chromium opens but shows "connection refused" | The app isn't up yet. The `cage` unit waits for it; for the desktop autostart, the service normally beats the GUI, but if not, add a small `sleep 5 &&` before `chromium-browser` in the autostart file. |
| Screen goes black after a few minutes | `sudo raspi-config` → Display Options → Screen Blanking → No, and confirm `unclutter`/`dpms = false` is in place. Reboot. |
| Pi throttles / gets hot | Use the active cooler or a fan case; `vcgencmd get_throttled` should report `0x0`. |

---

## 7. Day-to-day

- **Update the app:** `sudo -u energycenter -i`, `cd HomeEnergyCenter`,
  `git pull`, `source .venv/bin/activate`, `pip install -e .`,
  `alembic upgrade head`, `exit`, then `sudo systemctl restart energycenter`.
- **Go live:** once you trust the decisions in `/debug`, set
  `decision.dry_run: false` in `config.yaml` and restart the service.
- **Remote access from your phone:** install Tailscale on the Pi
  (`curl -fsSL https://tailscale.com/install.sh | sh` then `sudo tailscale up`)
  and on your phone — see `setup-guide.html` §8–9.
