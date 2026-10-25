# Diehl Platinum Inverter – MQTT / Home Assistant

> **Disclaimer:** Diehl, Diehl AKO and Platinum are trademarks of their respective owners.
> This project is not affiliated with, endorsed by, or supported by Diehl. All product
> names are used only to describe compatibility. Use at your own risk.

Reads a **Diehl AKO Platinum** PV inverter through its RS-232 service port and publishes
the values to MQTT with Home Assistant discovery. Developed and verified on a
**Platinum 4300 TL**; other Platinum TL models probably use the same protocol, but the
record layouts may differ.

All commands sent to the inverter are **read-only**.

## What you get in Home Assistant

| Sensor | Source | Update rate |
| --- | --- | --- |
| DC voltage, DC current, DC power | live value block 0x02 | every poll (default 10 s) |
| AC voltage, grid frequency | live value block 0x01 | every poll |
| AC power, AC current | newest interval record | ~30 s |
| Energy today, total energy | interval record + day summary | ~30 s |
| Coil / IGBT / grid monitoring / UI temperature | interval record | ~30 s |
| Isolation resistance, feed-in time today | interval record | ~30 s |
| Operating state (text and code), event code | interval record | ~30 s |

*Total energy* is suitable for the Home Assistant energy dashboard (`total_increasing`).

Notes:
- The inverter does not provide a live AC power value; AC power comes from the interval
  record and is therefore up to ~30 s old. It is never estimated.
- Coil and IGBT temperature read 0 below their measuring range; they are reported as
  unknown in that case.
- At night (inverter off) power and current are set to 0, measurements become unknown,
  and after midnight *energy today* is reset to 0.

## Hardware

- RS-232 service port of the inverter (DB9), **straight (non-crossed) serial cable**
- Any USB–RS-232 adapter with real ±12 V levels (developed with a Waveshare
  USB TO RS232/485/TTL, FTDI FT232RL)
- 19200 baud, 8N1

The RS-485 (EIA485) terminal of the inverter is Diehl's internal network bus and uses a
different, undocumented protocol; it is not used here.

## Repository layout

```
ct/diehl-platinum.sh                            Proxmox LXC installer (runs on the Proxmox host)
install/diehl-platinum-install.sh               install script (runs inside the container)
install/diehl-platinum/diehl_platinum_mqtt.py   the MQTT service
tools/                                          test and analysis tools
PROTOCOL.md                                     protocol documentation
```

## Running the service directly

```bash
pip install -r tools/requirements.txt paho-mqtt
python3 install/diehl-platinum/diehl_platinum_mqtt.py --port /dev/ttyUSB0 \
    --mqtt-host 192.168.1.10 --mqtt-user ha --mqtt-pass secret
```

Options can also be given as environment variables: `DIEHL_PORT`, `MQTT_HOST`,
`MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`, `POLL_INTERVAL`.

## Proxmox LXC container

The container script uses the framework of [community-scripts.org](https://community-scripts.org):
same wizard (default/advanced settings), same output, same `update` command inside the
container. It creates an **unprivileged** Debian container, passes the FTDI adapter through
via `dev0` (group `dialout`) and installs the service.

1. Fork or copy this repository to GitHub (it must be **public**, the installer fetches
   files from `raw.githubusercontent.com`).
2. In `ct/diehl-platinum.sh` replace `CHANGE_ME` in `_CS_DEFAULT_URL` with your GitHub user name.
3. On the Proxmox host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<user>/diehl-platinum-inverter-mqtt/main/ct/diehl-platinum.sh)"
```

The script detects the adapter under `/dev/serial/by-id/usb-FTDI*` and asks for the MQTT
broker, user and password. Unattended:

```bash
MQTT_HOST=192.168.1.10 MQTT_USER=ha MQTT_PASS=secret bash ct/diehl-platinum.sh
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `MQTT_HOST` | (prompt) | MQTT broker |
| `MQTT_PORT` | 1883 | |
| `MQTT_USER` / `MQTT_PASS` | (prompt) | empty user = no authentication |
| `POLL_INTERVAL` | 10 | poll interval in s |
| `DIEHL_SERIAL_DEV` | auto | adapter path, otherwise the only `/dev/serial/by-id/usb-FTDI*` |
| `var_cpu`, `var_ram`, `var_disk`, … | 1 / 256 / 2 | as for all community-scripts |

Inside the container: configuration in `/etc/diehl-platinum.env`, log with
`journalctl -u diehl-platinum -f`. To update the service, change `diehl_platinum_mqtt.py` in the
repository and run `update` inside the container.

If the adapter is not plugged in when the container starts, the container does not start.

## Tools

All in `tools/`, requiring `pyserial` (`pip install -r tools/requirements.txt`):

| Tool | Purpose |
| --- | --- |
| `diehl_ping.py` | connection test, day summary (`--day`), interval records (`--intv`, `--last`), value types (`--value`, `--value-scan`), all decoded where known |
| `diehl_live.py` | captures the live blocks 0x01/0x02 together with interval records as reference |
| `diehl_bridge.py` | serial bridge between a VM's virtual COM port and the real adapter, logs all traffic (used to observe the Diehl PLATINUM Service Tool) |

## Safety

Only read commands are used. The Diehl Service Tool also uses commands that access
parameters (`8D`, `4D`) and change communication settings (`AD`); write commands for grid
parameters very likely exist close to those. Do not experiment with unknown commands.

## Credits

- The frame format, CRC and the commands `2D`/`35` were first published by *stendec*
  (`RS_diehl.c`, 2014) for the Platinum 3800S.
- Record layouts for the 4300 TL, the live value blocks, addressing and additional
  commands were worked out for this project and verified against the export of the
  Diehl PLATINUM Service Tool.

## License

MIT
