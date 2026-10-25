#!/usr/bin/env python3
"""
Diehl Platinum 4300 TL -> MQTT / Home Assistant.

Reads the inverter through its RS-232 service port (19200 8N1) and publishes the values
via MQTT, including Home Assistant discovery. Every command sent to the inverter is a
read-only request.

Data sources:
  - newest interval record (every ~30 s): AC power, energy, temperatures, status, ...
  - live value blocks 0x02 / 0x01 (every poll): DC voltage/current/power, AC voltage,
    grid frequency
  - day summary of the previous day: energy counter at day end
    -> total energy = counter at end of yesterday + energy today

Usage:
  python3 diehl_platinum_mqtt.py --port /dev/ttyUSB0 --mqtt-host 192.168.1.10 \\
      --mqtt-user ha --mqtt-pass secret

  All options are also available as environment variables: DIEHL_PORT, MQTT_HOST,
  MQTT_PORT, MQTT_USER, MQTT_PASS, POLL_INTERVAL.

Requires: pip install pyserial paho-mqtt
"""
import argparse
import json
import logging
import os
import signal
import time
from datetime import date, timedelta
from typing import Optional

import paho.mqtt.client as mqtt
import serial

BAUD = 19200
ADDR = 0x13  # high nibble = destination unit (1 = UI/data logger), low nibble = sender
DEVICE_ID = "diehl_platinum_4300tl"
BASE_TOPIC = f"diehl/{DEVICE_ID}"

log = logging.getLogger("diehl")


# --------------------------------------------------------------------------- protocol

def crc16(data: bytes) -> bytes:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), high byte first."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc.to_bytes(2, "big")


def day_frame(d: date) -> bytes:
    body = bytes([0x2D, ADDR, 0x04, 0x13, d.year - 2000, d.month, d.day])
    return body + crc16(body)


def value_frame(vtype: int) -> bytes:
    body = bytes([0x35, ADDR, 0x01, vtype & 0xFF])
    return body + crc16(body)


def intv_frame(d: date, index: int) -> bytes:
    body = bytes([0x2D, ADDR, 0x06, 0x12, d.year - 2000, d.month, d.day,
                  (index >> 8) & 0xFF, index & 0xFF])
    return body + crc16(body)


def read_frame(port: serial.Serial) -> bytes:
    """Reads exactly one frame: 3 header bytes, then length + 2 CRC bytes.
    Returns as soon as the frame is complete instead of waiting for the timeout."""
    head = port.read(3)
    if len(head) < 3:
        return head
    return head + port.read(head[2] + 2)


class NoResponse(Exception):
    """The inverter does not answer at all (e.g. switched off at night)."""


class Inverter:
    def __init__(self, port: str):
        self.port_name = port
        self.port: Optional[serial.Serial] = None

    def _open(self) -> serial.Serial:
        if self.port is None or not self.port.is_open:
            self.port = serial.Serial(self.port_name, BAUD, bytesize=8, parity="N",
                                      stopbits=1, timeout=3.0, inter_byte_timeout=0.05)
        return self.port

    def close(self) -> None:
        if self.port is not None:
            try:
                self.port.close()
            finally:
                self.port = None

    def request(self, frame: bytes) -> Optional[bytes]:
        """Returns a valid data response (the command with inverted bits, e.g. 0xD2 for
        0x2D or 0xCA for 0x35), or None for 'no entry' (0x84).
        Raises NoResponse if nothing comes back at all. If the device answers 'busy'
        (0xD4), the request is repeated up to 10 times, like the Diehl Service Tool does.
        Timeout 3 s: day summaries sometimes take more than 1 s."""
        for _ in range(10):
            try:
                port = self._open()
                port.reset_input_buffer()
                port.write(frame)
                port.flush()
                resp = read_frame(port)
            except serial.SerialException:
                self.close()  # reopen on the next attempt (e.g. adapter unplugged)
                raise
            finally:
                time.sleep(0.05)
            if not resp:
                raise NoResponse()
            if len(resp) < 5 or crc16(resp[:-2]) != resp[-2:]:
                log.debug("invalid response: %s", resp.hex(" "))
                return None
            if resp[0] == 0xD4 and resp[2] == 0:
                continue  # busy -> ask again
            return resp if resp[0] == (frame[0] ^ 0xFF) else None
        log.debug("device stayed busy")
        return None

    def interval(self, d: date, index: int) -> Optional[dict]:
        r = self.request(intv_frame(d, index))
        if r is None or len(r) != 42:
            return None
        u16 = lambda i: int.from_bytes(r[i:i + 2], "big")
        le16 = lambda i: int.from_bytes(r[i:i + 2], "little")  # bytes 8-9, 30-37: little endian
        return {
            "time": f"{r[4]:02d}:{r[5]:02d}:{r[6]:02d}",
            "status": r[7],
            "status_text": status_text(r[7]),
            "event": le16(8),
            # coil/IGBT report 0 below their measuring range -> no value instead of 0 °C
            "temp_coil": r[24] or None,
            "temp_igbt": r[25] or None,
            "temp_gmu": r[26],
            "temp_ui": r[27],
            "feed_minutes": u16(28),
            "isolation": le16(30),
            "frequency": le16(32) / 10,
            "afi_current": le16(34),
            "dc_injection": le16(36),
            "dc_voltage": u16(10),
            "dc_current": r[12] / 10,
            "dc_power": u16(13),
            "ac_voltage": u16(15),
            "ac_current": r[17] / 10,
            "ac_power": u16(18),
            "energy_today": int.from_bytes(r[20:24], "big") / 1000,  # kWh
        }

    def live(self) -> dict:
        """Live values from the value blocks 0x02 (DC) and 0x01 (grid).
        Verified 2026-10-25: 0x02 word 0 = UDC in 0.1 V, word 1 = IDC in mA
        (U x I averaged over 30 s matches PDC of the interval record within +-3 W);
        0x01 word 0 = UAC in 0.1 V, word 1 = grid frequency in 0.1 Hz.
        The device does not provide a live AC power value in these blocks."""
        out = {}
        r = self.request(value_frame(0x02))
        if r is not None and len(r) >= 10:
            udc = int.from_bytes(r[4:6], "big") / 10
            idc = int.from_bytes(r[6:8], "big") / 1000
            out.update(dc_voltage=round(udc, 1), dc_current=round(idc, 3),
                       dc_power=round(udc * idc))
        r = self.request(value_frame(0x01))
        if r is not None and len(r) >= 10:
            out.update(ac_voltage=int.from_bytes(r[4:6], "big") / 10,
                       frequency=int.from_bytes(r[6:8], "big") / 10)
        return out

    def total_at_day_end(self, d: date) -> Optional[float]:
        """Total energy counter in kWh at the end of day d, None if there is no record."""
        r = self.request(day_frame(d))
        if r is None or len(r) != 80:
            return None
        return int.from_bytes(r[47:51], "big") / 1000

    def last_index(self, d: date) -> Optional[int]:
        """Newest existing interval index (exponential + binary search)."""
        if self.interval(d, 0) is None:
            return None
        lo, hi = 0, 1
        while hi <= 0xFFFF and self.interval(d, hi) is not None:
            lo, hi = hi, hi * 2
        hi = min(hi, 0x10000)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.interval(d, mid) is not None:
                lo = mid
            else:
                hi = mid
        return lo


# --------------------------------------------------------------------------- MQTT

# Operating state (byte 7 of the interval record). No Diehl list is known: the texts are
# derived from the course of 2026-10-24. Unknown codes are shown with their number.
STATUS_TEXT = {
    30: "Waiting/startup",
    31: "Isolation test",
    13: "Feeding in (ramp-up)",
    12: "Feeding in",
}
STATUS_OFF = "Off (no response)"


def status_text(code: int) -> str:
    return STATUS_TEXT.get(code, f"Unknown ({code})")


SENSORS = [
    # key, name, unit, device_class, state_class, display precision
    ("ac_power", "AC power", "W", "power", "measurement", 0),
    ("dc_power", "DC power", "W", "power", "measurement", 0),
    ("ac_voltage", "AC voltage", "V", "voltage", "measurement", 1),
    ("dc_voltage", "DC voltage", "V", "voltage", "measurement", 1),
    ("ac_current", "AC current", "A", "current", "measurement", 1),
    ("dc_current", "DC current", "A", "current", "measurement", 2),
    ("energy_today", "Energy today", "kWh", "energy", "total_increasing", 3),
    ("energy_total", "Total energy", "kWh", "energy", "total_increasing", 3),
    ("frequency", "Grid frequency", "Hz", "frequency", "measurement", 1),
    ("temp_coil", "Coil temperature", "°C", "temperature", "measurement", 0),
    ("temp_igbt", "IGBT temperature", "°C", "temperature", "measurement", 0),
    ("temp_gmu", "Grid monitoring temperature", "°C", "temperature", "measurement", 0),
    ("temp_ui", "User interface temperature", "°C", "temperature", "measurement", 0),
    ("isolation", "Isolation resistance", "kΩ", None, "measurement", 0),
    ("feed_minutes", "Feed-in time today", "min", "duration", "measurement", 0),
    ("status_text", "Operating state", None, None, None, 0),
    ("status", "Operating state (code)", None, None, None, 0),
    ("event", "Event", None, None, None, 0),
]


def publish_discovery(client: mqtt.Client) -> None:
    device = {
        "identifiers": [DEVICE_ID],
        "name": "Diehl Platinum inverter",
        "manufacturer": "Diehl AKO",
        "model": "Platinum 4300 TL",
    }
    for key, name, unit, dev_class, state_class, prec in SENSORS:
        cfg = {
            "name": name,
            "unique_id": f"{DEVICE_ID}_{key}",
            "object_id": f"{DEVICE_ID}_{key}",
            "state_topic": f"{BASE_TOPIC}/state",
            "value_template": f"{{{{ value_json.{key} }}}}",
            "availability_topic": f"{BASE_TOPIC}/availability",
            "device": device,
        }
        if unit:
            cfg["unit_of_measurement"] = unit
            cfg["suggested_display_precision"] = prec
        if dev_class:
            cfg["device_class"] = dev_class
        if state_class:
            cfg["state_class"] = state_class
        if key in ("status", "event", "isolation", "temp_gmu", "temp_ui"):
            cfg["entity_category"] = "diagnostic"
        client.publish(f"homeassistant/sensor/{DEVICE_ID}/{key}/config",
                       json.dumps(cfg), retain=True)


# --------------------------------------------------------------------------- main loop

class Bridge:
    def __init__(self, inverter: Inverter, client: mqtt.Client, interval: float):
        self.inv = inverter
        self.mqtt = client
        self.poll_interval = interval
        self.day: Optional[date] = None
        self.index: Optional[int] = None         # last interval index read today
        self.base_total: Optional[float] = None  # energy counter at end of previous day
        self.last_state: dict = {}
        self.offline_date: Optional[date] = None
        self.running = True

    def new_day(self, today: date) -> None:
        # Find the most recent day with a day summary (e.g. if yesterday had no data at all).
        # Raises NoResponse while the inverter is off -> retried on the next poll, because
        # self.day is only set afterwards.
        base = None
        for back in range(1, 8):
            total = self.inv.total_at_day_end(today - timedelta(days=back))
            if total is not None:
                base = total
                log.info("Energy counter at end of %s: %.3f kWh", today - timedelta(days=back), total)
                break
        log.info("New day %s", today)
        self.day, self.index, self.base_total = today, None, base

    def poll(self) -> None:
        today = date.today()
        if today != self.day:
            self.new_day(today)

        if self.index is None:
            self.index = self.inv.last_index(today)
            if self.index is None:
                log.info("No interval data for today yet")
                return
            log.info("Newest index today: %d", self.index)
            rec = self.inv.interval(today, self.index)
        else:
            # Read the records added since the last poll, keep the newest one
            rec = None
            for _ in range(200):
                nxt = self.inv.interval(today, self.index + 1)
                if nxt is None:
                    break
                self.index += 1
                rec = nxt

        # Base: new interval record (every ~30 s) or the last published state
        if rec is not None:
            state = dict(rec)
            if self.base_total is not None:
                state["energy_total"] = round(self.base_total + rec["energy_today"], 3)
            elif "energy_total" in self.last_state:
                state["energy_total"] = self.last_state["energy_total"]
        elif self.last_state:
            state = dict(self.last_state)
        else:
            return
        # Overlay live values on every poll (DC values, AC voltage, frequency)
        if state.get("status") in (12, 13):  # only while feeding in
            state.update(self.inv.live())
        self.publish(state)

    def publish(self, state: dict) -> None:
        self.last_state = state
        self.mqtt.publish(f"{BASE_TOPIC}/state", json.dumps(state), retain=True)
        log.debug("published %s", state)

    # Values nobody measures at night: report them as unknown instead of keeping the evening value
    NIGHT_UNKNOWN = ("dc_voltage", "ac_voltage", "frequency", "temp_coil", "temp_igbt",
                     "temp_gmu", "temp_ui", "isolation", "afi_current", "dc_injection",
                     "status", "event")

    def publish_offline_values(self) -> None:
        """Inverter off (night): power and current 0, measurements unknown, total energy kept.
        After midnight 'energy today' and the feed-in time are reset to 0 as well.
        Publishes at most once in the evening and once after midnight."""
        if not self.last_state:
            return
        today = date.today()
        if self.last_state.get("status_text") == STATUS_OFF and self.offline_date == today:
            return
        state = dict(self.last_state)
        for key in ("ac_power", "dc_power", "ac_current", "dc_current"):
            state[key] = 0
        for key in self.NIGHT_UNKNOWN:
            state[key] = None
        if self.day is not None and today != self.day:  # data is from yesterday
            state["energy_today"] = 0
            state["feed_minutes"] = 0
        state["status_text"] = STATUS_OFF
        self.offline_date = today
        self.publish(state)

    def run(self) -> None:
        silent_since: Optional[float] = None
        while self.running:
            try:
                self.poll()
                silent_since = None
                wait = self.poll_interval
            except NoResponse:
                if silent_since is None:
                    silent_since = time.monotonic()
                    log.info("No response from the inverter (off for the night?)")
                self.publish_offline_values()
                # after 10 min of silence only try every 5 min
                wait = 300 if time.monotonic() - silent_since > 600 else self.poll_interval
            except serial.SerialException as e:
                log.warning("Serial error: %s", e)
                wait = 30
            self._sleep(wait)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self.running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Diehl Platinum -> MQTT / Home Assistant")
    ap.add_argument("--port", default=env("DIEHL_PORT"), help="serial port")
    ap.add_argument("--mqtt-host", default=env("MQTT_HOST", "localhost"))
    ap.add_argument("--mqtt-port", type=int, default=int(env("MQTT_PORT", "1883")))
    ap.add_argument("--mqtt-user", default=env("MQTT_USER"))
    ap.add_argument("--mqtt-pass", default=env("MQTT_PASS"))
    ap.add_argument("--interval", type=float, default=float(env("POLL_INTERVAL", "10")),
                    help="poll interval in s (default 10)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if not args.port:
        ap.error("set --port or DIEHL_PORT")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=DEVICE_ID)
    if args.mqtt_user:
        client.username_pw_set(args.mqtt_user, args.mqtt_pass)
    client.will_set(f"{BASE_TOPIC}/availability", "offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection failed: %s", reason_code)
            return
        log.info("MQTT connected to %s:%d", args.mqtt_host, args.mqtt_port)
        publish_discovery(c)
        c.publish(f"{BASE_TOPIC}/availability", "online", retain=True)

    def on_disconnect(c, userdata, flags, reason_code, properties):
        log.warning("MQTT disconnected (%s) - reconnecting automatically", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    # Connect in the background: if the network is not up yet at startup or the broker is
    # briefly gone, paho retries on its own (1 s up to 2 min) instead of giving up.
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    client.connect_async(args.mqtt_host, args.mqtt_port, keepalive=60)
    client.loop_start()

    inverter = Inverter(args.port)
    bridge = Bridge(inverter, client, args.interval)

    def stop(*_):
        bridge.running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        bridge.run()
    finally:
        log.info("Shutting down ...")
        client.publish(f"{BASE_TOPIC}/availability", "offline", retain=True).wait_for_publish(2)
        client.loop_stop()
        client.disconnect()
        inverter.close()


if __name__ == "__main__":
    main()
