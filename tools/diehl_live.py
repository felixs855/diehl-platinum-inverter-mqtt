#!/usr/bin/env python3
"""
Capture tool for analysing the live value blocks (value types 0x01 and 0x02).

Queries the types 0x01 and 0x02 every --period seconds and, in parallel, reads every new
interval record (fully decoded) as a reference. Everything goes into a JSONL file for
later analysis. Read-only commands only.

Usage:
  python3 diehl_live.py /dev/ttyUSB0                 # 10 minutes
  python3 diehl_live.py /dev/ttyUSB0 --minutes 20 --period 2

Best run with changing clouds: varying power makes the field assignment unambiguous.
Requires diehl_ping.py in the same folder.
"""
import argparse
import json
import time
from datetime import date

import serial

import diehl_ping as dp


def request(port, frame: bytes, tries: int = 10) -> bytes:
    """Sends frame, repeats on 'busy' (D4). Returns a valid answer or b''."""
    for _ in range(tries):
        resp = dp.transfer(port, frame, verbose=False)
        time.sleep(0.05)
        if len(resp) < 5 or dp.crc16(resp[:-2]) != resp[-2:]:
            return b""
        if resp[0] == 0xD4 and resp[2] == 0:
            continue
        return resp
    return b""


def interval(port, addr: int, d: date, idx: int) -> bytes:
    r = request(port, dp.intv_frame(addr, d, idx))
    return r if len(r) == 42 and r[0] == 0xD2 else b""


def last_index(port, addr: int, d: date) -> int:
    if not interval(port, addr, d, 0):
        return -1
    lo, hi = 0, 1
    while hi <= 0xFFFF and interval(port, addr, d, hi):
        lo, hi = hi, hi * 2
    hi = min(hi, 0x10000)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if interval(port, addr, d, mid):
            lo = mid
        else:
            hi = mid
    return lo


def ref_values(r: bytes) -> dict:
    be = lambda i, n=2: int.from_bytes(r[i:i + n], "big")
    le = lambda i: int.from_bytes(r[i:i + 2], "little")
    return {
        "time": f"{r[4]:02d}:{r[5]:02d}:{r[6]:02d}", "state": r[7],
        "udc": be(10), "idc": r[12] / 10, "pdc": be(13),
        "uac": be(15), "iac": r[17] / 10, "pac": be(18), "e_day": be(20, 4),
        "t_coil": r[24], "t_igbt": r[25], "t_gmu": r[26], "t_ui": r[27],
        "riso": le(30), "freq": le(32) / 10, "afi": le(34), "idc_ac": le(36),
    }


def words(r: bytes) -> list:
    """Payload without the echoed type as 16-bit words (big endian) for the console."""
    p = r[4:-2]
    return [int.from_bytes(p[i:i + 2], "big") for i in range(0, len(p) - 1, 2)]


def main():
    ap = argparse.ArgumentParser(description="Capture live blocks 0x01/0x02 + interval reference")
    ap.add_argument("port")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--period", type=float, default=2.0, help="interval of the live queries in s")
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x13)
    ap.add_argument("--log", default="live_log.jsonl")
    args = ap.parse_args()

    with serial.Serial(args.port, dp.BAUD, timeout=3.0, inter_byte_timeout=0.05) as port, \
            open(args.log, "a") as log:
        today = date.today()
        print("Searching for the newest interval record ...")
        idx = last_index(port, args.addr, today)
        if idx < 0:
            print("No interval data for today yet - is the inverter running?")
            return
        print(f"Newest index: {idx}. Capturing for {args.minutes:g} min, stop with Ctrl+C\n")

        end = time.time() + args.minutes * 60
        pac = "?"
        try:
            while time.time() < end:
                t = time.time()
                row = {"t": round(t, 2)}
                for vtype in (0x01, 0x02):
                    r = request(port, dp.value_frame(args.addr, vtype))
                    row[f"v{vtype:02x}"] = dp.hexs(r)
                # read a new interval record if there is one
                nxt = interval(port, args.addr, today, idx + 1)
                while nxt:
                    idx += 1
                    ref = ref_values(nxt)
                    pac = ref["pac"]
                    log.write(json.dumps({"t": round(time.time(), 2), "idx": idx, "ref": ref,
                                          "hex": dp.hexs(nxt)}) + "\n")
                    print(f"  reference {ref['time']}: PAC {ref['pac']} W, PDC {ref['pdc']} W, "
                          f"UDC {ref['udc']} V, UAC {ref['uac']} V, f {ref['freq']} Hz")
                    nxt = interval(port, args.addr, today, idx + 1)
                log.write(json.dumps(row) + "\n")
                log.flush()
                w1 = words(bytes.fromhex(row["v01"].replace(" ", ""))) if row["v01"] else []
                w2 = words(bytes.fromhex(row["v02"].replace(" ", ""))) if row["v02"] else []
                print(f"{time.strftime('%H:%M:%S')}  (last PAC {pac} W)  "
                      f"0x01: {' '.join(map(str, w1[:8]))}  |  0x02: {' '.join(map(str, w2[:8]))}")
                time.sleep(max(0.0, args.period - (time.time() - t)))
        except KeyboardInterrupt:
            print("\nAborted.")
    print(f"\nDone. Capture written to {args.log}.")


if __name__ == "__main__":
    main()
