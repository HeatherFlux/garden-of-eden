# Accessing the unit after flashing

## 1. Flash with SSH + networking enabled (first access)

You need SSH to run `bin/setup.sh` the first time, so enable it when you flash
the card. In the **Raspberry Pi Imager**, click the ⚙ (advanced options) and set:

- **Enable SSH** (password or public-key)
- **Username / password** (e.g. user `gardyn`)
- **Hostname**: `gardyn`
- **Wi‑Fi** SSID + password + country

> Headless alternative (no Imager UI): after flashing, on the boot partition
> create an empty file named `ssh`, plus a `userconf.txt` and `wpa_supplicant.conf`.
> See the Raspberry Pi headless setup docs.

Then boot the Pi and connect:

```bash
ssh gardyn@gardyn.local
```

## 2. Install — SSH stays on, mDNS is added

`bin/setup.sh` keeps SSH enabled and installs `avahi-daemon` + sets the hostname,
so the unit is reliably reachable at **`gardyn.local`** afterward (override with
`GARDEN_HOSTNAME=… ./bin/setup.sh`). It also installs **`garden-api.service`**,
which serves the web UI/REST API on boot.

```bash
git clone https://github.com/iot-root/garden-of-eden.git
cd garden-of-eden
cp .env-dist .env && nano .env
./bin/setup.sh
```

## 3. Use it

- **Web UI:** http://gardyn.local:5000/
- **REST API:** same host, e.g. `curl http://gardyn.local:5000/system`
- **SSH:** `ssh gardyn@gardyn.local`
- **Home Assistant:** auto-discovers over MQTT (see the README).

If `gardyn.local` doesn't resolve (some Android/Windows setups), use the Pi's IP
(`hostname -I` over SSH, or check your router). To lock down the API over the
network, set `GARDEN_API_KEY` in `.env` and enter it in the web UI's ⚙ settings.

## Services

```bash
sudo systemctl status garden-api.service   # web UI + REST
sudo systemctl status mqtt.service         # MQTT / Home Assistant
sudo systemctl status ssh                  # remote access
```
