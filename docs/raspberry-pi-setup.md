# Raspberry Pi 5 — setup, install, autostart, kiosk display & Tailscale

A start-to-finish walkthrough for running **HomeEnergyCenter** on a Raspberry
Pi 5: flashing the OS, installing the app, starting it automatically at boot,
showing the dashboard full-screen ("frameless") on an attached monitor, and
reaching it from your phone over Tailscale.

This guide is written for the layout actually used on this device:

| Thing | Value |
|---|---|
| Hostname | `HomeCenter` (so `HomeCenter.local` on the LAN) |
| Admin / imager account | `alex` (has `sudo`) |
| Service account that owns the app | `homecenter` (home dir `/opt/homecenter`) |
| App directory | `/opt/homecenter/HomeEnergyCenter` |
| Virtualenv | `/opt/homecenter/HomeEnergyCenter/.venv` |
| Config file | `/opt/homecenter/HomeEnergyCenter/config.yaml` |
| Web dashboard | port `8000` |
| OS | Raspberry Pi OS (Debian *trixie*), Python 3.13 |

> Two accounts are used on purpose: `alex` for everything that needs `sudo`, and
> a dedicated `homecenter` system user that owns the app files and runs the
> service. The `homecenter` user has `/opt/homecenter` as its home — not
> `/home/homecenter` — because it was created as a system user with an explicit
> `--home-dir` (see §4.0).

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
   - **Username / password:** `alex` (or whatever admin name you prefer) + a
     password you'll remember. **Do not leave it as `pi` / `raspberry`.** This is
     the everyday admin account — the dedicated `homecenter` service user gets
     created later in §4.0. *(Tip: Raspberry Pi OS defaults to a UK keyboard
     layout — if your password contains `@` or `"` and you'll be typing it on a
     directly-attached keyboard, those two keys are swapped vs. a US layout. Set
     the keyboard layout in this same dialog to avoid surprises.)*
   - **Wireless LAN:** only if you can't use Ethernet (Ethernet preferred).
   - **Locale:** time zone `Europe/Brussels`.
   - **Services tab → Enable SSH** (password or, better, public-key).
6. **Save → Write.** When done: insert the card, connect Ethernet (+ monitor &
   keyboard if doing the kiosk), power on. First boot takes a minute or two while
   it resizes the filesystem and reboots.

---

## 3. First login & system update

From your laptop, log in as the admin user you set in the imager:

```bash
ssh alex@HomeCenter.local
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

### 4.0 Create the `homecenter` service user (one-time, as admin)

The app runs under its own dedicated account with `/opt/homecenter` as its home —
this keeps the orchestrator separate from your normal user data and makes the
systemd unit hardening (§5) tighter. From the `alex` shell:

```bash
sudo useradd --system --create-home --home-dir /opt/homecenter --shell /bin/bash homecenter
sudo -u homecenter -i           # you're now homecenter, in /opt/homecenter
```

The rest of §4 runs as `homecenter` in `/opt/homecenter`.

### 4.1 Get the code

```bash
cd ~                            # = /opt/homecenter
git clone <your-repo-url> HomeEnergyCenter      # → /opt/homecenter/HomeEnergyCenter
cd HomeEnergyCenter
```

Or copy it from your Windows PC with `rsync` (run from WSL / Git Bash on the PC),
excluding local junk:

```bash
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.db' \
  ./HomeEnergyCenter/ homecenter@HomeCenter.local:/opt/homecenter/HomeEnergyCenter/
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
WorkingDirectory=/opt/homecenter/HomeEnergyCenter
Environment=EO_CONFIG=/opt/homecenter/HomeEnergyCenter/config.yaml
ExecStart=/opt/homecenter/HomeEnergyCenter/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
# Light hardening — it only needs LAN access and its own data dir.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/homecenter/HomeEnergyCenter

[Install]
WantedBy=multi-user.target
```

> **Path warning:** every path in this unit must actually exist on disk.
> Verify it before reloading systemd:
>
> ```bash
> getent passwd homecenter                                       # 6th field = home dir
> ls -d /opt/homecenter/HomeEnergyCenter/.venv/bin/python        # must succeed
> ```
>
> If systemd later logs `Failed to set up mount namespacing … No such file or
> directory` and `status=226/NAMESPACE`, it means one of the `/opt/homecenter/…`
> paths in this file doesn't exist — fix the unit, `sudo systemctl daemon-reload`,
> `sudo systemctl restart homeenergycenter`.

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

### 7.3 Screen orientation (landscape / portrait)

If you're using a touchscreen mounted in portrait — or just want to flip the
default orientation — set it with `wlr-randr` and persist it from the same
`labwc` autostart file you use for the kiosk.

**Find the output name** (from a graphical session terminal):

```bash
wlr-randr
```

You'll see something like `HDMI-A-1` (HDMI monitor), `DSI-1` (official 7"
touchscreen), or `DPI-1`. Note the exact name.

**Try a rotation live** to confirm it looks right:

```bash
wlr-randr --output HDMI-A-1 --transform 90        # portrait, top of screen on the left
wlr-randr --output HDMI-A-1 --transform 270       # portrait, top of screen on the right
wlr-randr --output HDMI-A-1 --transform 180       # upside-down landscape
wlr-randr --output HDMI-A-1 --transform normal    # default landscape
```

The screen rotates instantly. Tap around — Wayland re-maps touch coordinates to
the rotated output automatically, so taps should land under your finger. (If
they don't, see the troubleshooting table.)

**Persist across reboots** — add the chosen rotation to
`~/.config/labwc/autostart`, *before* the `chromium-browser` line so the
dashboard renders into the rotated output from the start:

```sh
wlr-randr --output HDMI-A-1 --transform 90 &
unclutter --timeout 1 &
chromium-browser --kiosk --noerrdialogs --disable-infobars --no-first-run \
  --check-for-update-interval=31536000 \
  --app=http://localhost:8000 &
```

> **GUI alternative (desktop image only):** top-left menu →
> **Preferences → Screen Configuration**, right-click the monitor →
> **Orientation → Right / Left / Inverted / Normal**, **Apply**, then **Save**
> (writes the right per-compositor config file for you).

> **Official 7" DSI touchscreen, 180° only:** if all you need is to flip it
> upside down for a stand with the ribbon at the top, the firmware can do it
> on its own — add `lcd_rotate=2` to `/boot/firmware/config.txt` and reboot.
> For 90 / 270° stick with `wlr-randr`; the firmware rotate parameters are
> deprecated on the Pi 5 KMS stack for arbitrary angles.

### 7.4 Reboot and check

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
| `status=226/NAMESPACE` + "Failed to set up mount namespacing: … No such file or directory" | A path in the unit file doesn't exist. Confirm with `getent passwd homecenter` (home dir = 6th field) and `ls -d /opt/homecenter/HomeEnergyCenter/.venv/bin/python`. Fix the `WorkingDirectory` / `Environment=EO_CONFIG` / `ExecStart` / `ReadWritePaths` lines, then `sudo systemctl daemon-reload && sudo systemctl restart homeenergycenter`. |
| `Address already in use` on port 8000 | Both the systemd service *and* a manual `python main.py` are running. Stop one. Find the listener: `sudo ss -ltnp | grep 8000`. |
| `HomeCenter.local` doesn't resolve | Use the IP from your router. On the Pi: `ip -4 addr show` (look for the `192.168.129.x` address). |
| Dashboard tile says a device is unreachable | Open `http://HomeCenter.local:8000/debug` — the health panel shows, per device, whether it's configured and reachable on its port. Fix the IP/token in `config.yaml`, then `sudo systemctl restart homeenergycenter`. |
| Works on LAN but not over Tailscale | Confirm `web.host: 0.0.0.0` in `config.yaml` (not `127.0.0.1`). Check the Pi shows `connected` in `tailscale status` on your phone, and that the phone's Tailscale VPN toggle is on. If `ufw` is active, see §6.4. |
| `tailscale up` won't authenticate | Make sure the install script finished (`apt install tailscale` step). Re-run `sudo tailscale up` and open the printed URL in a browser where you're logged into the right Tailscale account. |
| Chromium opens but "connection refused" | The app isn't up yet. The `cage` unit waits for it; for the desktop autostart files, prefix the `chromium-browser` line with `sleep 5 &&` if it loses the race at boot. |
| Screen goes black after a few minutes | `raspi-config` → Display Options → Screen Blanking → No; confirm `unclutter` / `dpms = false` in place; reboot. |
| Screen rotated correctly but touch is in the wrong place | Wayland normally re-maps touch with the output, but on some no-name USB touchscreens the touch device isn't bound to the output. Find the touch device with `libinput list-devices \| grep -A1 -i touch`, then bind it to the output in `~/.config/labwc/rc.xml` (`<map-to-output>HDMI-A-1</map-to-output>` in the libinput section) and reboot. The official Pi DSI touchscreen and the Waveshare DSI panels don't need this. |
| Pi throttles / gets hot | Use the active cooler or fan case. `vcgencmd get_throttled` should report `0x0`. |

---

## 9. Day-to-day

### 9.1 Update the app

Run this whenever you want to pull new commits. The order matters: stop the
service *before* touching files, otherwise the running Python holds the SQLite DB
and a half-pulled tree can leave the next start in a weird state.

```bash
# 1. Stop the service.
sudo systemctl stop homeenergycenter

# 2. Switch to the service account.
sudo -u homecenter -i
cd ~/HomeEnergyCenter           # = /opt/homecenter/HomeEnergyCenter

# 3. Make sure no local edits will block the pull.
git status                      # expect "nothing to commit, working tree clean"
                                # (config.yaml is gitignored — it won't show up)

# 4. Pull latest code (fast-forward only — refuses if history has diverged).
git pull --ff-only

# 5. Refresh Python dependencies and apply any new DB migrations.
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -e .
alembic upgrade head

# 6. Back to the admin shell, restart, verify.
exit
sudo systemctl start homeenergycenter
sudo systemctl status homeenergycenter        # expect "active (running)"
journalctl -u homeenergycenter -n 50 --no-pager
curl -sI http://localhost:8000/               # expect HTTP/1.1 200 OK
```

Common snags during an update:

- **`git pull` → "Your local changes would be overwritten":** you've edited a
  tracked file on the Pi. `git status` lists them. Either `git stash`, pull,
  `git stash pop`; or `git restore <file>` to discard. Keep all your settings in
  `config.yaml` (gitignored) — never edit tracked source on the Pi.
- **Service fails to start after the update:**
  `journalctl -u homeenergycenter -n 100 --no-pager`. The two usual causes are a
  new required field in `config.yaml` (compare against `config.example.yaml`),
  and a forgotten `alembic upgrade head`.
- **If you originally installed via `rsync` from your Windows PC** instead of
  `git clone`, step 4 is `rsync ...` from the same source — everything else is
  identical.

### 9.2 Other routine bits

- **Go live:** once you trust the decisions shown in `/debug`, set
  `decision.dry_run: false` in `config.yaml` and `sudo systemctl restart homeenergycenter`.
- **Update Tailscale:** picks up updates with the rest of the system on
  `sudo apt update && sudo apt upgrade`.
- **Bookmark on your phone:** `http://homecenter.<your-tailnet>.ts.net:8000` →
  Add to Home Screen — opens like an app.
