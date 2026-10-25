#!/usr/bin/env python3
"""
Test and exploration tool for Diehl Platinum inverters (RS-232 service port).
Sends the init packet, day summary requests (--day), interval record requests
(--intv, --last) or value requests (--value, --value-scan) and shows the raw answers,
decoded where the layout is known. All commands are read-only.

Usage:
  python3 diehl_ping.py /dev/ttyUSB0                  # init packet, address 0x13 (default)
  python3 diehl_ping.py /dev/ttyUSB0 --addr 0x14      # other address
  python3 diehl_ping.py /dev/ttyUSB0 --scan           # try all addresses 0x00-0xFF
  python3 diehl_ping.py /dev/ttyUSB0 --baud 9600      # other baud rate
  python3 diehl_ping.py /dev/ttyUSB0 --day            # day summary of yesterday
  python3 diehl_ping.py /dev/ttyUSB0 --day --date 2026-10-22
  python3 diehl_ping.py /dev/ttyUSB0 --intv           # today, index 0-4
  python3 diehl_ping.py /dev/ttyUSB0 --intv 10 --count 3
  python3 diehl_ping.py /dev/ttyUSB0 --intv --count 0 # up to the end of the list
  python3 diehl_ping.py /dev/ttyUSB0 --last           # newest 3 interval records
  python3 diehl_ping.py /dev/ttyUSB0 --value-scan     # values 0x00-0xFF, 2 passes
  python3 diehl_ping.py /dev/ttyUSB0 --value 0x02 --repeat 10   # watch one value type

Requires: pip install pyserial
"""
import argparse
import time
from datetime import date, timedelta
from typing import Optional
import serial

BAUD = 19200
DEFAULT_ADDR = 0x13  # high nibble = destination unit (1 = UI/data logger), low nibble = sender


def crc16(data: bytes) -> bytes:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), high byte first."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc.to_bytes(2, "big")


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def init_frame(addr: int) -> bytes:
    body = bytes([0xC5, addr, 0x03, 0x00, 0x00, 0xC3])
    return body + crc16(body)  # address 0x13 -> C5 13 03 00 00 C3 6B 5A


def day_frame(addr: int, d: date) -> bytes:
    """Read the day summary (command 0x2D, sub-command 0x13); read-only."""
    body = bytes([0x2D, addr, 0x04, 0x13, d.year - 2000, d.month, d.day])
    return body + crc16(body)


def intv_frame(addr: int, d: date, index: int) -> bytes:
    """Read one interval record (command 0x2D, sub-command 0x12); read-only."""
    body = bytes([0x2D, addr, 0x06, 0x12, d.year - 2000, d.month, d.day,
                  (index >> 8) & 0xFF, index & 0xFF])
    return body + crc16(body)


def read_frame(port: serial.Serial) -> bytes:
    """Reads exactly one frame: 3 header bytes, then length + 2 CRC bytes.
    Returns as soon as the frame is complete instead of waiting for the timeout."""
    head = port.read(3)
    if len(head) < 3:
        return head
    return head + port.read(head[2] + 2)


def transfer(port: serial.Serial, frame: bytes, verbose: bool) -> bytes:
    port.reset_input_buffer()
    if verbose:
        print(f"TX: {hexs(frame)}")
    port.write(frame)
    port.flush()
    return read_frame(port)


def run_intv(port: serial.Serial, addr: int, d: date, start: int, count: int) -> None:
    """Reads interval records starting at `start`; count=0 reads up to the end of the list."""
    idx = start
    lengths = {}
    last = None
    while count == 0 or idx < start + count:
        frame = intv_frame(addr, d, idx)
        resp = transfer(port, frame, verbose=False)
        ok = len(resp) >= 3 and crc16(resp[:-2]) == resp[-2:]
        tag = "CRC OK" if ok else ("no response" if not resp else "CRC ERROR")
        print(f"[{idx:4d}] TX: {hexs(frame)}")
        print(f"       RX: {hexs(resp)}  ({len(resp)} bytes)  {tag}")
        if not ok:
            print("Stopped: no valid response.")
            break
        if resp[0] != 0xD2:  # not a regular data response -> most likely end of list
            print(f"Response code 0x{resp[0]:02X} instead of 0xD2 -> most likely end of list.")
            break
        lengths[len(resp)] = lengths.get(len(resp), 0) + 1
        last = (idx, resp)
        idx += 1
        time.sleep(0.05)
    if last:
        print(f"\n{idx - start} records read, lengths: {lengths}")
        print(f"Last record: index {last[0]}\n  {hexs(last[1])}")


def decode_intv(r: bytes) -> list:
    """Interval record Platinum 4300 TL (42 bytes). Fully verified on 2026-10-25:
    37 records of 2026-10-24 match the export of the PLATINUM Service Tool in every field.
    Note: bytes 8-9 and 30-37 are little endian, everything else big endian."""
    be = lambda i, n=2: int.from_bytes(r[i:i + n], "big")
    le = lambda i: int.from_bytes(r[i:i + 2], "little")
    return [
        ("Time", f"{r[4]:02d}:{r[5]:02d}:{r[6]:02d}"),
        ("Operating state", f"{r[7]}"),
        ("Event", f"{le(8)}"),
        ("DC voltage", f"{be(10)} V"),
        ("DC current", f"{r[12] / 10:.1f} A"),
        ("DC power", f"{be(13)} W"),
        ("AC voltage", f"{be(15)} V"),
        ("AC current", f"{r[17] / 10:.1f} A"),
        ("AC power", f"{be(18)} W"),
        ("Energy today so far", f"{be(20, 4) / 1000:.3f} kWh"),
        ("Temp. coil/IGBT/GMU/UI", f"{r[24]} / {r[25]} / {r[26]} / {r[27]} °C"),
        ("Feed-in time today", f"{be(28)} min"),
        ("Isolation resistance", f"{le(30)} kOhm"),
        ("Grid frequency", f"{le(32) / 10:.1f} Hz"),
        ("Residual current (AFI)", f"{le(34)} mA"),
        ("DC injection into AC", f"{le(36)} mA"),
    ]


def intv_exists(port: serial.Serial, addr: int, d: date, idx: int) -> Optional[bytes]:
    """Returns the record if the index exists (response code 0xD2, CRC OK), else None."""
    resp = transfer(port, intv_frame(addr, d, idx), verbose=False)
    time.sleep(0.05)
    if len(resp) >= 3 and crc16(resp[:-2]) == resp[-2:] and resp[0] == 0xD2:
        return resp
    return None


def run_last(port: serial.Serial, addr: int, d: date, n: int) -> None:
    """Finds the newest interval record by binary search and prints the last n."""
    if intv_exists(port, addr, d, 0) is None:
        print("Index 0 does not exist - no interval data for this day.")
        return
    lo, hi = 0, 1
    while hi <= 0xFFFF and intv_exists(port, addr, d, hi) is not None:  # find upper bound
        lo, hi = hi, hi * 2
        print(f"  index {lo} exists ...")
    hi = min(hi, 0x10000)
    while hi - lo > 1:  # lo exists, hi does not
        mid = (lo + hi) // 2
        if intv_exists(port, addr, d, mid) is not None:
            lo = mid
        else:
            hi = mid
    print(f"Newest index: {lo}\n")
    for idx in range(max(0, lo - n + 1), lo + 1):
        resp = intv_exists(port, addr, d, idx)
        if resp:
            t = f"{resp[4]:02d}:{resp[5]:02d}:{resp[6]:02d}"
            print(f"[{idx:5d}] {t}  RX: {hexs(resp)}")
    if resp and len(resp) == 42:
        print("\nNewest record decoded:")
        for name, value in decode_intv(resp):
            print(f"  {name:24} {value}")


def value_frame(addr: int, vtype: int) -> bytes:
    """Read a single value or value block (command 0x35 'getValue')."""
    body = bytes([0x35, addr, 0x01, vtype & 0xFF])
    return body + crc16(body)


def get_value(port: serial.Serial, addr: int, vtype: int) -> bytes:
    resp = transfer(port, value_frame(addr, vtype), verbose=False)
    time.sleep(0.05)
    return resp


def value_info(resp: bytes, vtype: Optional[int] = None) -> str:
    """Short description of a response, payload also shown as an integer."""
    if not resp:
        return "no response"
    if len(resp) < 5 or crc16(resp[:-2]) != resp[-2:]:
        return f"CRC ERROR  {hexs(resp)}"
    payload = resp[3:-2]
    txt = f"code {resp[0]:02X}  len {resp[2]:3d}  data: {hexs(payload)}"
    if vtype is not None and payload[:1] == bytes([vtype]):
        payload = payload[1:]  # strip the echoed type in front of the value
    if 1 <= len(payload) <= 8:
        txt += f"   | BE {int.from_bytes(payload, 'big')}  LE {int.from_bytes(payload, 'little')}"
    return txt


def run_value_scan(port: serial.Serial, addr: int, start: int, end: int, passes: int) -> None:
    results = []
    for p in range(passes):
        print(f"Pass {p + 1}/{passes} ...")
        cur = {}
        for vtype in range(start, end + 1):
            print(f"\r  type 0x{vtype:02X}", end="", flush=True)
            cur[vtype] = get_value(port, addr, vtype)
        print("\r" + " " * 20 + "\r", end="")
        results.append(cur)
        if p + 1 < passes:
            time.sleep(5)
    print(f"{'Type':>5}  {'':3}  Response (* = value changed between passes)")
    for vtype in range(start, end + 1):
        resps = [r[vtype] for r in results]
        last = resps[-1]
        if not last or (len(last) >= 1 and last[0] == 0x84 and len(last) == 5):
            continue  # no response or 'no entry'
        changed = "*" if len(set(resps)) > 1 else " "
        print(f" 0x{vtype:02X}  {changed:3}  {value_info(last, vtype)}")
    nothing = [v for v in range(start, end + 1) if not results[-1][v]]
    rejected = [v for v in range(start, end + 1)
                if results[-1][v] and results[-1][v][0] == 0x84 and len(results[-1][v]) == 5]
    print(f"\nno response: {len(nothing)} types, rejected (0x84): {len(rejected)} types")


def run_value_watch(port: serial.Serial, addr: int, vtype: int, repeat: int, pause: float) -> None:
    for i in range(repeat):
        resp = get_value(port, addr, vtype)
        print(f"{time.strftime('%H:%M:%S')}  type 0x{vtype:02X}  {value_info(resp, vtype)}")
        if i + 1 < repeat:
            time.sleep(pause)


def ping(port: serial.Serial, addr: int, verbose: bool, day: bool = False,
         d: Optional[date] = None) -> bytes:
    """Sends the init packet (or a day summary request) to addr and returns the raw answer."""
    d = d or date.today() - timedelta(days=1)
    frame = day_frame(addr, d) if day else init_frame(addr)
    return transfer(port, frame, verbose)


def report(addr: int, resp: bytes) -> None:
    crc = ""
    if len(resp) >= 3:
        crc = "CRC OK" if crc16(resp[:-2]) == resp[-2:] else "CRC ERROR"
    print(f"Address 0x{addr:02X} ({addr:3d})  RX: {hexs(resp)}  ({len(resp)} bytes)  {crc}")
    if len(resp) == 80 and resp[0] == 0xD2 and resp[3] == 0x13 and crc.endswith("OK"):
        for name, value in decode_day(resp):
            print(f"  {name:30} {value}")


def decode_day(r: bytes) -> list:
    """Day summary Platinum 4300 TL (80 bytes). Layout follows the column order of the
    PLATINUM Service Tool export; all 40 values of 2026-10-24 match the export exactly.
    All values big endian. Byte 77 has always been 0 so far."""
    u16 = lambda i: int.from_bytes(r[i:i + 2], "big")
    u32 = lambda i: int.from_bytes(r[i:i + 4], "big")
    t = lambda i: f"{r[i]} / {r[i + 1]} / {r[i + 2]} °C"
    return [
        ("Date", f"20{r[4]:02d}-{r[5]:02d}-{r[6]:02d}"),
        ("Energy of the day", f"{u32(31) / 1000:.3f} kWh"),
        ("Total energy (end of day)", f"{u32(47) / 1000:.3f} kWh"),
        ("Total operating time", f"{u32(51)} min ({u32(51) / 60:.0f} h)"),
        ("DC voltage max/MPP max/avg", f"{u16(7)} / {u16(9)} / {u16(11)} V"),
        ("DC current max/avg", f"{r[13] / 10:.1f} / {r[14] / 10:.1f} A"),
        ("DC power max/avg", f"{u16(15)} / {u16(17)} W"),
        ("AC voltage max/min/avg", f"{u16(19)} / {u16(21)} / {u16(23)} V"),
        ("AC current max/avg", f"{r[25] / 10:.1f} / {r[26] / 10:.1f} A"),
        ("AC power max/avg", f"{u16(27)} / {u16(29)} W"),
        ("T coil max/min/avg", t(35)),
        ("T IGBT max/min/avg", t(38)),
        ("T GMU max/min/avg", t(41)),
        ("T UI max/min/avg", t(44)),
        ("Isolation max/min", f"{u16(55)} / {u16(57)} kOhm"),
        ("Grid frequency max/min/avg", f"{u16(59) / 10:.1f} / {u16(61) / 10:.1f} / {u16(63) / 10:.1f} Hz"),
        ("Residual current max/min/avg", f"{u16(65)} / {u16(67)} / {u16(69)} mA"),
        ("DC injection max/min/avg", f"{u16(71)} / {u16(73)} / {u16(75)} mA"),
    ]


def main():
    ap = argparse.ArgumentParser(description="Diehl Platinum test tool (read-only)")
    ap.add_argument("port", help="serial port, e.g. /dev/ttyUSB0 or /dev/cu.usbserial-XXXX")
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=DEFAULT_ADDR,
                    help="device address (default 0x13)")
    ap.add_argument("--scan", action="store_true",
                    help="try all addresses 0x00-0xFF")
    ap.add_argument("--day", action="store_true",
                    help="request yesterday's day summary instead of the init packet")
    ap.add_argument("--intv", type=int, nargs="?", const=0, default=None, metavar="START",
                    help="read interval records starting at index START (default 0)")
    ap.add_argument("--value-scan", nargs="*", type=lambda x: int(x, 0), metavar="START END",
                    help="query value types (0x35), default 0x00-0xFF")
    ap.add_argument("--passes", type=int, default=2, help="passes for --value-scan")
    ap.add_argument("--value", type=lambda x: int(x, 0), default=None,
                    help="query one value type repeatedly")
    ap.add_argument("--repeat", type=int, default=10, help="repetitions for --value")
    ap.add_argument("--pause", type=float, default=2.0, help="pause in s for --value")
    ap.add_argument("--last", type=int, nargs="?", const=3, default=None, metavar="N",
                    help="find the newest interval record by binary search, show the last N")
    ap.add_argument("--count", type=int, default=5,
                    help="number of interval records (0 = up to the end of the list)")
    ap.add_argument("--date", type=date.fromisoformat, default=None,
                    help="date as YYYY-MM-DD (default: yesterday for --day, today for --intv)")
    ap.add_argument("--baud", type=int, default=BAUD,
                    help="baud rate (default 19200)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="response timeout in s (default 3.0, 0.3 for scans)")
    args = ap.parse_args()

    fast = args.scan or args.value_scan is not None
    timeout = args.timeout if args.timeout is not None else (0.3 if fast else 3.0)

    with serial.Serial(args.port, args.baud, bytesize=8, parity="N",
                       stopbits=1, timeout=timeout, inter_byte_timeout=0.05) as port:
        if args.value_scan is not None:
            rng = args.value_scan or [0x00, 0xFF]
            start, end = (rng[0], rng[0]) if len(rng) == 1 else (rng[0], rng[1])
            run_value_scan(port, args.addr, start, end, args.passes)
            return

        if args.value is not None:
            run_value_watch(port, args.addr, args.value, args.repeat, args.pause)
            return

        if args.last is not None:
            run_last(port, args.addr, args.date or date.today(), args.last)
            return

        if args.intv is not None:
            run_intv(port, args.addr, args.date or date.today(), args.intv, args.count)
            return

        if not args.scan:
            resp = ping(port, args.addr, verbose=True, day=args.day, d=args.date)
            if resp:
                report(args.addr, resp)
            else:
                print("RX: no response (check cable, GND, baud rate; inverter switched on?)")
            return

        print(f"Scanning 256 addresses, timeout {timeout} s per address ...")
        found = []
        try:
            for addr in range(256):
                print(f"\r  0x{addr:02X}", end="", flush=True)
                resp = ping(port, addr, verbose=False, day=args.day, d=args.date)
                if resp:
                    print("\r", end="")
                    report(addr, resp)
                    found.append(addr)
                time.sleep(0.05)  # short pause to let the line settle
        except KeyboardInterrupt:
            print("\nAborted.")

        print("\r" + " " * 10)
        if found:
            print("Response from: " + ", ".join(f"0x{a:02X}" for a in found))
        else:
            print("No response on any address.")


if __name__ == "__main__":
    main()
