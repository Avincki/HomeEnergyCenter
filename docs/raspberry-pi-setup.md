# Raspberry Pi 5 — setup, install, autostart, kiosk display & Tailscale

A start-to-finish walkthrough for running **HomeEnergyCenter** on a Raspberry
Pi 5: flashing the OS, installing the app, starting it automatically at boot,
showing the dashboard full-screen ("frameless") on an attached monitor, and
reaching it from your phone over Tailscale.

This guide is written for the layout actually used on this device:

| Thing | Value |
|---|---|
| Hostname | `HomeCenter` (so `HomeCenter.local` on the LAN) |
| Login user that owns the app | `homecenter` |
| App directory | `/home/homecenter/HomeEnergyCenter` |
| Virtualenv | `/home/homecenter/HomeEnergyCenter/.venv` |
| Config file | `/home/homecenter/HomeEnergyCenter/config.yaml` |
| Web dashboard | port `8000` |
| OS | Raspberry Pi OS (Debian *trixie*), Python 3.13 |

> Any `sudo` command below can be run by any account with admin rights (on this
> Pi that's the `alex` account). The `homecenter` account owns and runs the app
> itself.

> **Network requirement:** the Pi must be on the **same LAN** as your inverter,
> battery and meters (this project assumes the `192.168.129.0/24` subnet). Modbus
> and the local device APIs are not reachable from outside the home network — a
> cloud host cannot work. Tailscale (below) gives you *remote* access to the
> dashboard, but the Pi itself still has to sit on the home LAN.

---

## 1. What you need

- Raspberry Pi 5 (4 GB is plenty) + official 27 W USB-C PSU + active cooler / fan case.
- A 32 GB+ A2-class microSD card (or, better for 24/7 longevity, an SSD on USB-3).
- Ethernet cable to your home router (preferred over Wi-Fi for an always-on box).
- For the kiosk display: a monitor + micro-HDMI→HDMI cable, and a keyboard for first boot.
- Your `config.yaml` (or `config.example.yaml` as a starting point) with device
  IPs, the ENTSO-E API token, and tariff settings.
- A free Tailscale account — <https://tailscale.com> — for phone access.
- Another computer to run **Raspberry Pi Imager** and to SSH in.

---

## 2. Flash Raspberry Pi OS

1. Install **Raspberry Pi Imager** from <https://www.raspberrypi.com/software/>
   on your laptop/PC and launch it.
2. **Choose Device:** Raspberry Pi 5.
3. **Choose OS:**
   - If you want the dashboard on an attached screen (the kiosk part, §7) →
     **Raspberry Pi OS (64-bit)** — the *full desktop* edition (includes Chromium
     and a graphical session, which kiosk mode needs).
   - If you only ever want headless access (LAN + phone via Tailscale) →
     **Raspberry Pi OS Lite (64-bit)** is enough.
4. **Choose Storage:** your microSD card / SSD.
5. Click **Next → Edit Settings** and pre-configure:
   - **Hostname:** `HomeCenter`
   - **Username / password:** `homecenter` + a password you'll remember. **Do not
     leave it as `pi` / `raspberry`.** *(Tip: Raspberry Pi OS defaults to a UK
     keyboard layout — if your password contains `@` or `"` and you'll be typing
     it on a directly-attached keyboard, those two keys are swapped vs. a US
     layout. Set the keyboard layout in this same dialog to avoid surprises.)*
   - **Wireless LAN:** only if you can't use Ethernet (Ethernet preferred).
   - **Locale:** time zone `Europe/Brussels`.
   - **Services tab → Enable SSH** (password or, better, public-key).
6. **Save → Write.** When done: insert the card, connect Ethernet (+ monitor &
   keyboard if doing the kiosk), power on. First boot takes a minute or two while
   it resizes the filesystem and reboots.

---

## 3. First login & system update

From your laptop:

```bash
ssh homecenter@HomeCenter.local
```

(If `.local` doesn't resolve, find the Pi's IP on your router and use that.)

Update everything and install build prerequisites:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
    python3 python3-venv python3-pip python3-dev \
    git build-essential libssl-dev libffi-dev \
    sqlite3 curl ca-certificates
python3 --version          # 3.11+ (trixie ships 3.13)
sudo reboot                # if the kernel/firmware was updated
```

Then open `sudo raspi-config` and set:
- **Display Options → Screen Blanking → No** — so a wall display never sleeps.
- (Kiosk only) **System Options → Boot / Auto Login → Desktop Autologin** — boots
  straight into the graphical session without a login prompt.

---

## 4. Install HomeEnergyCenter

All of this runs as the **`homecenter`** user, in its home directory.

### 4.1 Get the code

```bash
cd ~
git clone <your-repo-url> HomeEnergyCenter      # → /home/homecenter/HomeEnergyCenter
cd HomeEnergyCenter
```

Or copy it from your Windows PC with `rsync` (run from WSL / Git Bash on the PC),
excluding local junk:

```bash
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.db' \
  ./HomeEnergyCenter/ homecenter@HomeCenter.local:/home/homecenter/HomeEnergyCenter/
```

### 4.2 Virtualenv + install

```bash
cd ~/HomeEnergyCenter
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -e .
```

(If `pip` falls back to building `pymodbus` / `aiohttp` from source, give it
10–15 minutes — one-off.)

### 4.3 Configure

```bash
cp config.example.yaml config.yaml
nano config.yaml
```

At minimum set:
- **Device IPs** — sonnenBatterie, P1 meter, car charger, small/large solar,
  SolarEdge inverter (use the static IPs you reserved on the router).
- **`prices.api_key`** — your ENTSO-E token.
- **`web:`** — keep `host: 0.0.0.0`, `port: 8000`. Binding to `0.0.0.0` (not
  `127.0.0.1`) is what makes the dashboard reachable from the LAN *and* over the
  Tailscale interface.
- Keep **`decision.dry_run: true`** for the first weeks — it suppresses all
  actuator writes to the inverter while you watch it behave.

### 4.4 Initialise the database

```bash
alembic upgrade head
```

**If you see `sqlite3.OperationalError: table readings already exists`:** a stale
`data/orchestrator.db` from an earlier interrupted run is in the way. On a fresh
install it has nothing worth keeping — delete it and re-run:

```bash
rm data/orchestrator.db
alembic upgrade head
```

*(Only if a DB ever has real data you want to keep: `alembic stamp 0001` to
bookmark the existing schema, then `alembic upgrade head` to apply the rest.
Not the case on a fresh install.)*

### 4.5 Smoke test

```bash
python main.py
```

You should see structured logs from the device pollers and uvicorn announcing
it's listening on `0.0.0.0:8000`. From any device on the LAN open
`http://HomeCenter.local:8000/` — the dashboard should load. Hit `Ctrl+C` to
stop, then continue to §5 to make it start on its own.

---

## 5. Run it automatically at boot (systemd service)

A systemd unit gives you auto-start at boot, auto-restart on crash, and logs in
`journalctl`. Create it as an admin user:

```bash
sudo nano /etc/systemd/system/homeenergycenter.service
```

```ini
[Unit]
Description=HomeEnergyCenter (energy orchestrator)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=homecenter
Group=homecenter
WorkingDirectory=/home/homecenter/HomeEnergyCenter
Environment=EO_CONFIG=/home/homecenter/HomeEnergyCenter/config.yaml
ExecStart=/home/homecenter/HomeEnergyCenter/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
# Light hardening — it only needs LAN access and its own data dir.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=tmpfs
ReadWritePaths=/home/homecenter/HomeEnergyCenter

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now homeenergycenter.service
sudo systemctl status homeenergycenter.service        # expect "active (running)"
journalctl -u homeenergycenter.service -f             # live logs; Ctrl+C to stop watching
```

It now starts on every boot, before you log in, and restarts itself if it
crashes. Handy later:

| Action | Command |
|---|---|
| Restart after a config change | `sudo systemctl restart homeenergycenter` |
| Stop | `sudo systemctl stop homeenergycenter` |
| Disable autostart | `sudo systemctl disable homeenergycenter` |
| Last 200 log lines | `journalctl -u homeenergycenter -n 200 --no-pager` |

> Run the smoke test (`python main.py`) **and** this service at the same time and
> you'll get `Address already in use` on port 8000 — only one of them should be
> running. Once the service is up, you don't run `main.py` by hand any more.

---

## 6. Remote access with Tailscale

Tailscale puts the Pi on a private encrypted network ("tailnet") so you can open
the dashboard from your phone on any connection, without exposing anything to the
public internet.

### 6.1 Install (system-wide — nothing to do with the Python venv)

Run as an admin user:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

This adds Tailscale's apt repo, installs the `tailscale` package, and enables the
`tailscaled` systemd service — so Tailscale itself also comes back after a reboot.

### 6.2 Authenticate

```bash
sudo tailscale up
```

It prints a login URL — open it in any browser, sign in to your Tailscale
account, approve the device. Then confirm:

```bash
tailscale status
tailscale ip -4          # the Pi's 100.x.y.z address (e.g. 100.78.17.68)
```

### 6.3 Reach the dashboard from your phone

1. Install **Tailscale** from the App Store / Play Store, sign in with the same
   account, toggle the VPN **on** (approve the VPN permission prompt).
2. Open a browser and go to the Pi's tailnet address:
   - By IP: `http://100.x.y.z:8000`
   - By MagicDNS name (if MagicDNS is enabled for your tailnet — it usually is):
     `http://homecenter:8000`, or the fully-qualified
     `http://homecenter.<your-tailnet>.ts.net:8000` (best to bookmark — won't
     collide with `.local` mDNS suffixes on other networks).
3. (Optional) Add it to your home screen: iOS Safari → Share → *Add to Home
   Screen*; Android Chrome → menu → *Install app*.

### 6.4 Firewall

Raspberry Pi OS has no firewall enabled by default — nothing to do. If you've
turned on `ufw`, allow port 8000 from the LAN and the Tailscale CGNAT range:

```bash
sudo ufw allow from 192.168.129.0/24 to any port 8000 proto tcp
sudo ufw allow from 100.64.0.0/10   to any port 8000 proto tcp
```

---

## 7. Show the dashboard full-screen ("frameless")

This needs the **desktop** image (§2) with **Desktop Autologin** enabled (§3).
The idea: when the graphical session starts, launch Chromium in kiosk mode
pointed at `http://localhost:8000` — full screen, no tabs, no address bar, no
window borders.

Raspberry Pi OS *trixie* on a Pi 5 runs a Wayland session; depending on the image
the compositor is **labwc** (newer) or **wayfire** (older). Do the one that
matches your Pi.

### 7.1 Hide the mouse cursor (optional)

```bash
sudo apt install -y unclutter
```

(Screen blanking should already be off from `raspi-config` in §3.)

### 7.2a If your session is **labwc**

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

### 7.2b If your session is **wayfire**

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

### 7.2c (Alternative) Lite image + `cage`

If you used the **Lite** image and want the most minimal kiosk, run Chromium
under the `cage` compositor as its own service. Create
`/etc/systemd/system/kiosk.service`:

```ini
[Unit]
Description=Chromium kiosk
After=homeenergycenter.service systemd-user-sessions.service
Wants=homeenergycenter.service

[Service]
User=homecenter
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
sudo apt install -y cage chromium-browser
sudo loginctl enable-linger homecenter
sudo systemctl enable --now kiosk.service
```

### 7.3 Reboot and check

```bash
sudo reboot
```

After the reboot the monitor comes up straight into the dashboard, full screen,
no browser chrome. To get out: SSH in and `pkill chromium` (or
`sudo systemctl stop kiosk` for the `cage` setup), or press `Ctrl+Alt+F2` for a
text console.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `sudo` says **"Sorry, try again"** | Wrong password for that account. The prompt is invisible — no characters echo, that's normal. Check Caps Lock. If you set the password on a US-layout laptop but type it on a UK-layout Pi keyboard, `@`/`"` are swapped. If `homecenter` lacks admin rights, run `sudo` as the `alex` account instead. |
| `alembic upgrade head` → **`table readings already exists`** | Stale DB from an interrupted run. On a fresh install: `rm data/orchestrator.db && alembic upgrade head`. |
| Service won't start | `journalctl -u homeenergycenter -n 100 --no-pager` — usually a `config.yaml` validation error, or a wrong path in the unit file. |
| `Address already in use` on port 8000 | Both the systemd service *and* a manual `python main.py` are running. Stop one. Find the listener: `sudo ss -ltnp | grep 8000`. |
| `HomeCenter.local` doesn't resolve | Use the IP from your router. On the Pi: `ip -4 addr show` (look for the `192.168.129.x` address). |
| Dashboard tile says a device is unreachable | Open `http://HomeCenter.local:8000/debug` — the health panel shows, per device, whether it's configured and reachable on its port. Fix the IP/token in `config.yaml`, then `sudo systemctl restart homeenergycenter`. |
| Works on LAN but not over Tailscale | Confirm `web.host: 0.0.0.0` in `config.yaml` (not `127.0.0.1`). Check the Pi shows `connected` in `tailscale status` on your phone, and that the phone's Tailscale VPN toggle is on. If `ufw` is active, see §6.4. |
| `tailscale up` won't authenticate | Make sure the install script finished (`apt install tailscale` step). Re-run `sudo tailscale up` and open the printed URL in a browser where you're logged into the right Tailscale account. |
| Chromium opens but "connection refused" | The app isn't up yet. The `cage` unit waits for it; for the desktop autostart files, prefix the `chromium-browser` line with `sleep 5 &&` if it loses the race at boot. |
| Screen goes black after a few minutes | `raspi-config` → Display Options → Screen Blanking → No; confirm `unclutter` / `dpms = false` in place; reboot. |
| Pi throttles / gets hot | Use the active cooler or fan case. `vcgencmd get_throttled` should report `0x0`. |

---

## 9. Day-to-day

- **Update the app:**
  ```bash
  sudo -u homecenter -i
  cd ~/HomeEnergyCenter && git pull
  source .venv/bin/activate && pip install -e . && alembic upgrade head
  exit
  sudo systemctl restart homeenergycenter
  ```
- **Go live:** once you trust the decisions shown in `/debug`, set
  `decision.dry_run: false` in `config.yaml` and `sudo systemctl restart homeenergycenter`.
- **Update Tailscale:** it updates with the rest of the system on `sudo apt upgrade`.
- **Bookmark on your phone:** `http://homecenter.<your-tailnet>.ts.net:8000` →
  Add to Home Screen — opens like an app.
