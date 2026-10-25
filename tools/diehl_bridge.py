#!/usr/bin/env python3
"""
Serial bridge with capture: virtual machine COM port (socket) <-> USB serial adapter.

The Windows program (PLATINUM Service Tool) talks through the virtual COM port of the VM;
this script forwards every byte to the real adapter and logs both directions with
timestamps. Frames with a valid Diehl CRC are printed decoded, other bytes as raw data.
When the tool switches the inverter to a higher baud rate (command AD .. 02 08 <code>),
the bridge follows automatically once the inverter has acknowledged.

Parallels Desktop (VM shut down): Configuration -> Hardware -> + -> Serial Port,
source "New socket", name "tcp://127.0.0.1:2020", mode "Server". Then:

       python3 diehl_bridge.py /dev/cu.usbserial-XXXX --socket tcp://127.0.0.1:2020

Alternatively a Unix socket path, or --listen (the script is the server, Parallels the client).

Capture: bridge_log.jsonl (one line per data chunk) + console.
Requires: pip install pyserial
"""
import argparse
import json
import os
import select
import socket
import time

import serial

BAUD = 19200
# Baud rate codes of the command AD xx 02 08 <code>. 04 = 115200 is confirmed by the
# Service Tool ("Set Baudrate 115200"); the other values are an assumption.
BAUD_CODES = {0x00: 9600, 0x01: 19200, 0x02: 38400, 0x03: 57600, 0x04: 115200}
IDLE_REVERT_S = 20  # after this much silence go back to the initial baud rate


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc.to_bytes(2, "big")


def hexs(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


class FrameSplitter:
    """Reassembles Diehl frames ([cmd][addr][len][data...][crc16]) from a byte stream."""

    def __init__(self):
        self.buf = bytearray()
        self.junk = bytearray()  # bytes that do not belong to a valid frame

    def flush_junk(self) -> bytes:
        """After a pause in transmission: hand out everything unprocessed as raw data."""
        out = bytes(self.junk + self.buf)
        self.junk.clear()
        self.buf.clear()
        return out

    def feed(self, data: bytes) -> list:
        self.buf += data
        frames = []
        while len(self.buf) >= 5:
            need = 3 + self.buf[2] + 2
            if len(self.buf) < need:
                break
            cand = bytes(self.buf[:need])
            if crc16(cand[:-2]) == cand[-2:]:
                frames.append(cand)
                del self.buf[:need]
            else:
                self.junk.append(self.buf[0])  # out of sync: put the byte aside
                del self.buf[0]
        return frames


def open_socket(path: str, listen: bool) -> socket.socket:
    if path.startswith("tcp://"):
        host, port = path[6:].rsplit(":", 1)
        host, port = host.strip("[]"), int(port)
        if listen:
            srv = socket.create_server((host, port))
            print(f"Waiting for the VM on {path} ...")
            conn, _ = srv.accept()
            srv.close()
            return conn
        while True:
            try:
                s = socket.create_connection((host, port), timeout=5)
                s.settimeout(None)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                return s
            except OSError:
                print(f"{path} not reachable - is the VM running? Retrying in 2 s")
                time.sleep(2)
    if listen:
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        print(f"Waiting for the VM on {path} ...")
        conn, _ = srv.accept()
        srv.close()
        return conn
    while True:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            return s
        except OSError:
            print(f"Socket {path} does not exist yet - is the VM running? Retrying in 2 s")
            time.sleep(2)


def main():
    ap = argparse.ArgumentParser(description="Serial bridge VM <-> adapter with capture")
    ap.add_argument("port", help="real adapter, e.g. /dev/cu.usbserial-XXXX")
    ap.add_argument("--socket", default="tcp://127.0.0.1:2020",
                    help="VM serial socket: tcp://host:port or path of a Unix socket")
    ap.add_argument("--listen", action="store_true",
                    help="the script creates the socket (VM is the client)")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--log", default="bridge_log.jsonl")
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N", stopbits=1, timeout=0)
    sock = open_socket(args.socket, args.listen)
    print(f"Connected: VM <-> {args.port} @ {args.baud} baud. Capture in {args.log}. Stop with Ctrl+C\n")

    split = {"PC>INV": FrameSplitter(), "INV>PC": FrameSplitter()}
    t0 = time.monotonic()
    last_rx = {"PC>INV": 0.0, "INV>PC": 0.0}
    pending_baud = None  # code from an AD command, applied only after the acknowledgement

    def set_baud(new: int, why: str) -> None:
        ser.flush()
        ser.baudrate = new
        print(f"{time.monotonic() - t0:9.3f}  ==> adapter set to {new} baud ({why})")
        log.write(json.dumps({"t": round(time.monotonic() - t0, 3), "baud": new}) + "\n")
    with open(args.log, "a") as log:
        try:
            while True:
                r, _, _ = select.select([sock, ser.fileno()], [], [], 0.05)
                now = time.monotonic()
                if ser.baudrate != args.baud and now - max(last_rx.values()) > IDLE_REVERT_S:
                    set_baud(args.baud, f"{IDLE_REVERT_S} s without traffic")
                for d, sp in split.items():  # after a 150 ms pause show raw bytes
                    if now - last_rx[d] > 0.15 and (sp.junk or sp.buf):
                        print(f"{now - t0:9.3f}  {d}  ?? {hexs(sp.flush_junk())}   (no valid frame)")
                if sock in r:
                    try:
                        data = sock.recv(4096)
                    except ConnectionResetError:
                        data = b""
                    if not data:
                        print("The VM closed the socket.")
                        break
                    ser.write(data)
                    direction = "PC>INV"
                elif ser.fileno() in r:
                    data = ser.read(4096)
                    if not data:
                        continue
                    sock.sendall(data)
                    direction = "INV>PC"
                else:
                    continue
                last_rx[direction] = time.monotonic()
                t = last_rx[direction] - t0
                log.write(json.dumps({"t": round(t, 3), "dir": direction, "hex": hexs(data)}) + "\n")
                log.flush()
                for frame in split[direction].feed(data):
                    print(f"{t:9.3f}  {direction}  {hexs(frame)}")
                    # baud rate switch: PC sends AD xx 02 08 <code>, inverter acknowledges 52 xx 01 08
                    if direction == "PC>INV" and frame[0] == 0xAD and frame[2] == 2 and frame[3] == 0x08:
                        pending_baud = frame[4]
                    elif direction == "INV>PC" and frame[0] == 0x52 and pending_baud is not None:
                        new = BAUD_CODES.get(pending_baud)
                        if new:
                            set_baud(new, f"switch command code 0x{pending_baud:02X}")
                        else:
                            print(f"           !!  unknown baud code 0x{pending_baud:02X} - adapter unchanged")
                        pending_baud = None
        except KeyboardInterrupt:
            print("\nStopped.")
    sock.close()
    ser.close()


if __name__ == "__main__":
    main()
