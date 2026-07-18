# Direct ESP32-H2 Matter-over-Thread PoC

Minimal direct controller for Matter-over-Thread lights:

```text
Python Matter controller <-> matter0 TUN <-> USB/SLIP <-> ESP32-H2/OpenThread <-> bulbs
```

There is no OTBR, Docker, forwarding configuration, Ethernet route, or Wi-Fi
route. The H2 is a Full Thread Device and forms the Thread network. `matter0`
is only an IPv6 endpoint for the local Python process.

The host associates each Matter node ID with its Thread EUI-64 while
commissioning, then persists the stable mesh-local address learned during CASE.
Because these bulbs do not answer operational mDNS on the minimal mesh, the
bridge answers the controller's address-resolution query locally. Matter CASE
still authenticates each bulb's operational certificate; only discovery is
replaced.

## Files

- `firmware/`: ESP-IDF H2 firmware (OpenThread plus a small IPv6/SLIP bridge)
- `matter_h2.py`: host bridge, Matter commissioner, and On/Off CLI
- `state/`: persistent Matter fabric, device mapping, and controller state

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

Check that the H2 has attached as a Thread router or leader (formation can take
about a minute after an H2 reset):

```bash
uv run --python 3.12 python matter_h2.py radio
```

Factory-reset only the new bulb, then commission it. The next free Matter node
ID is selected automatically. Omitting `--code` avoids placing the setup code
in shell history:

```bash
uv run --python 3.12 python matter_h2.py commission
```

List bulbs (`*` marks the default, normally the last commissioned bulb):

```bash
uv run --python 3.12 python matter_h2.py list
```

Control endpoint 1 (the usual On/Off endpoint for a bulb):

```bash
uv run --python 3.12 python matter_h2.py on --node 1
uv run --python 3.12 python matter_h2.py off --node 2
```

Omit `--node` to control the default bulb.

Remove the persistent local TUN device:

```bash
uv run --python 3.12 python matter_h2.py cleanup
```

The commands use `sudo` only for `ip tuntap`, interface configuration, and
unblocking Bluetooth. They never enable IPv4 or IPv6 forwarding.
