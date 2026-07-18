# Direct ESP32-H2 Matter-over-Thread PoC

Minimal direct controller for one Matter-over-Thread light:

```text
Python Matter controller <-> matter0 TUN <-> USB/SLIP <-> ESP32-H2/OpenThread <-> bulb
```

There is no OTBR, Docker, forwarding configuration, Ethernet route, or Wi-Fi
route. The H2 is a Full Thread Device and forms the Thread network. `matter0`
is only an IPv6 endpoint for the local Python process.

The host learns the bulb's current Thread RLOC directly from the H2. Because
this bulb does not answer operational mDNS on the minimal mesh, the bridge
answers the controller's address-resolution query locally. Matter CASE still
authenticates the bulb's operational certificate; only discovery is replaced.

## Files

- `firmware/`: ESP-IDF H2 firmware (OpenThread plus a small IPv6/SLIP bridge)
- `matter_h2.py`: host bridge, Matter commissioner, and On/Off CLI
- `state/`: persistent Thread-independent Matter fabric/controller state

## Build and flash

This uses the existing IDF checkout at `~/Code/esp-idf`:

```bash
cd ~/Code/matter-h2-direct/firmware
. ~/Code/esp-idf/export.sh
idf.py set-target esp32h2
idf.py -p /dev/ttyACM0 erase-flash flash
```

Erasing the H2 creates a new Thread dataset. A bulb commissioned with the old
dataset must then be factory-reset and commissioned again.

## Python environment

The project is pinned to the newest Matter wheel that works with Debian 11's
glibc. Resolve it with:

```bash
cd ~/Code/matter-h2-direct
uv sync --python 3.12
```

The native Matter wheel expects `/data`; on this host it is a symlink to the
project's `state` directory:

```text
/data -> /home/pi/Code/matter-h2-direct/state
```

## Commands

Check that the H2 has become Thread leader (formation can take about a minute
after an H2 reset):

```bash
uv run --python 3.12 python matter_h2.py radio
```

Factory-reset the bulb, then commission it. Omitting `--code` avoids placing the
setup code in shell history:

```bash
uv run --python 3.12 python matter_h2.py commission
```

Control endpoint 1 (the usual On/Off endpoint for a bulb):

```bash
uv run --python 3.12 python matter_h2.py on
uv run --python 3.12 python matter_h2.py off
```

Remove the persistent local TUN device:

```bash
uv run --python 3.12 python matter_h2.py cleanup
```

The commands use `sudo` only for `ip tuntap`, interface configuration, and
unblocking Bluetooth. They never enable IPv4 or IPv6 forwarding.
