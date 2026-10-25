# Diehl Platinum serial protocol (RS-232 service port)

Status: verified on a Platinum 4300 TL (October 2026). Each field is marked as
**verified** (checked against the Diehl PLATINUM Service Tool export, the display or
internal consistency), **high** (physically consistent across several records) or
**hypothesis**.

## Physical layer

- DB9 service port, RS-232 levels, straight (non-crossed) cable
- 19200 baud, 8N1, no flow control
- The Service Tool temporarily switches to 115200 baud for bulk reads (command `AD`, see below)

## Frame format

Requests and responses share the same layout. Multi-byte values are big endian unless noted.

| Byte | Request | Response |
| --- | --- | --- |
| 0 | command (e.g. `2D`) | command with inverted bits (`2D` → `D2`), or a status code |
| 1 | address: high nibble = destination, low nibble = sender | destination and sender swapped |
| 2 | payload length | payload length |
| 3 … n−3 | payload, starting with the sub-command or type | payload, starting with the echoed sub-command or type |
| n−2, n−1 | CRC, high byte first | CRC, high byte first |

**CRC:** CRC-16/CCITT-FALSE over all bytes before the CRC (polynomial 0x1021, initial value
0xFFFF, no reflection, no final XOR).

```python
def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc.to_bytes(2, "big")
```

**Status codes:** `84` (usually length 0) = no entry / unknown type. `D4` with length 0 =
busy; the Service Tool repeats the request until data arrives.

**Addressing:** the PC uses sender `F` (the Service Tool sends `1F`, the answer comes with
`F1`). Any sender nibble works, the device answers to it (e.g. `13` → `31`). The inverter
contains three units:

| Unit | Function |
| --- | --- |
| 1 | UI – user interface with the data logger (all data below comes from here) |
| 2 | ICU – control unit |
| 3 | GMU – grid monitoring unit |

## Commands

Read commands used by this project:

| Command | Example request (with CRC) | Response | Purpose |
| --- | --- | --- | --- |
| `C5` | `C5 13 03 00 00 C3 6B 5A` | `3A 31 00 F8 5C` (empty ack) | connection test / init |
| `2D` / `13` | `2D 13 04 13 1A 0A 17 A1 94` (2026-10-23) | 80 bytes | day summary |
| `2D` / `12` | `2D 13 06 12 1A 0A 18 00 00 E4 00` (2026-10-24, index 0) | 42 bytes | interval record, 16-bit index |
| `35` | `35 13 01 02 …` | depends on type | single value / value block |
| `25` | `25 1F 00` | 5 bytes BCD, e.g. `73 30 24 06 06` | firmware version of the unit (733024-6) |

Dates are sent as year − 2000, month, day.

Commands observed from the Service Tool (not used here):

| Command | Example | Response | Meaning |
| --- | --- | --- | --- |
| `15` | `15 1F 00` | `0F 0C 0B 0E 00` | unknown |
| `65` | `65 1F 04 11 02 80 5B` | n bytes | raw memory read: count, 3-byte address |
| `8D` | `8D 3F 02 03 0A` | ack `72` | probably unlocks parameter access |
| `4D` | `4D 3F 03 02 00 08` | 8 words | read GMU parameters: block, word offset, count |
| `AD` | `AD 1F 02 08 04` | ack `52 F1 01 08` | set baud rate, `04` = 115200 |
| `D5` | `D5 1F 00` | ack `2A` | probably end of session |

`8D`, `4D` and `AD` access settings; write commands for grid parameters very likely exist
nearby. Do not send them.

## Day summary (`2D`/`13`, 80 bytes)

Fully mapped: the 40 columns of the Service Tool export fill bytes 7–76 in order, and all
40 values of one day match the export exactly. All values big endian. The response can
take more than 1 s (use a 3 s timeout). For the current day the answer is `84`.

| Bytes | Type | Content | Unit |
| --- | --- | --- | --- |
| 4–6 | 3×u8 | date (YY MM DD) | – |
| 7–12 | 3×u16 | UDC max, UDC MPP max, UDC average | V |
| 13–14 | 2×u8 | IDC max, IDC average | 0.1 A |
| 15–18 | 2×u16 | PDC max, PDC average | W |
| 19–24 | 3×u16 | UAC max, UAC min, UAC average | V |
| 25–26 | 2×u8 | IAC max, IAC average | 0.1 A |
| 27–30 | 2×u16 | PAC max, PAC average | W |
| 31–34 | u32 | day energy | Wh |
| 35–46 | 12×u8 | T_COIL, T_IGBT, T_GMU, T_UI, each max / min / average | °C |
| 47–50 | u32 | total energy at end of day | Wh |
| 51–54 | u32 | total operating time | min |
| 55–58 | 2×u16 | isolation resistance max, min | kΩ |
| 59–64 | 3×u16 | grid frequency max, min, average | 0.1 Hz |
| 65–70 | 3×u16 | AFI residual current max, min, average | mA |
| 71–76 | 3×u16 | DC injection into AC max, min, average | mA |
| 77 | u8 | always 0 so far | – |

Cross-check: total energy at the end of day N = total energy at the end of day N−1 + day
energy of day N, exactly to the Wh.

## Interval record (`2D`/`12`, 42 bytes)

Fully mapped and verified: 37 records matched the Service Tool export in all 18 columns.
**Bytes 8–9 and 30–37 are little endian**, everything else big endian.

| Bytes | Type | Service Tool column | Content | Unit |
| --- | --- | --- | --- | --- |
| 4–6 | 3×u8 | Time | time hh mm ss | – |
| 7 | u8 | Operating state | see below | – |
| 8–9 | u16 LE | Event | event code (e.g. 402 at startup) | – |
| 10–11 | u16 | UDC | DC voltage | V |
| 12 | u8 | IDC | DC current | 0.1 A |
| 13–14 | u16 | PDC | DC power | W |
| 15–16 | u16 | UAC | AC voltage | V |
| 17 | u8 | IAC | AC current | 0.1 A |
| 18–19 | u16 | PAC | AC power | W |
| 20–23 | u32 | Day energy | energy today so far | Wh |
| 24 | u8 | T_COIL | coil temperature (0 below measuring range) | °C |
| 25 | u8 | T_IGBT | IGBT temperature (0 below measuring range) | °C |
| 26 | u8 | T_GMU | grid monitoring unit temperature | °C |
| 27 | u8 | T_UI | user interface temperature | °C |
| 28–29 | u16 | Operating time | feed-in time today | min |
| 30–31 | u16 LE | R isolation | isolation resistance (50000 = upper limit) | kΩ |
| 32–33 | u16 LE | Frequency | grid frequency | 0.1 Hz |
| 34–35 | u16 LE | AFI | residual current | mA |
| 36–37 | u16 LE | I DC at AC | DC injection into AC | mA |
| 38–39 | – | – | always 0 so far | – |

**Operating states** (no Diehl list known, meaning derived from observation): 30 waiting /
startup, 31 isolation test, 13 feeding in (ramp-up, very low power), 12 feeding in.

**Index and timing:** index 0 is the first record of the day (inverter start in the
morning). Records are stored every ~11 s at first and every ~30 s later; according to
Diehl the logger keeps about 30 days. An index beyond the newest record returns `84`, so
the newest record can be found by binary search in about 25 requests.

**Live total energy** = total energy from the previous day's summary + energy today so far.

## Value types (`35`)

16 of 256 types return data; the others are rejected with `84`. The payload starts with
the echoed type.

| Type | Content | Status |
| --- | --- | --- |
| 0x01 | live grid block: word 0 = UAC in 0.1 V, word 1 = grid frequency in 0.1 Hz, word 6 = T_GMU in 0.1 °C, word 5 = 0x8000 (sensor without value). Words 0, 2 and 4 are AC voltages, probably from the three-phase grid monitoring | high |
| 0x02 | live DC block: word 0 = UDC in 0.1 V, word 1 = IDC in mA; U × I averaged over 30 s matches PDC of the interval record within ±3 W. Words 2/3 unknown (not AC power) | verified |
| 0x07 | `06 03`, probably device family / model | hypothesis |
| 0x10 | AC voltage | V, high |
| 0x12, 0x14, 0x1F | probably temperatures in °C | hypothesis |
| 0x15 | device clock: YY MM DD hh mm ss | verified |
| 0x20 / 0x80 | 15 bytes, constant | device identification |
| 0x22 / 0x82 | 8 bytes, constant; the Service Tool shows them as the raw serial number | verified |
| 0x11, 0x16, 0x1E, 0x91 | constant 0 or 1 | unknown |

No live AC power value has been found (not in 0x01, 0x02, 0x11 or 0x1E).

## Open points

- live AC power; meaning of words 2/3 in block 0x02
- complete list of operating states and event codes
- event memory: read by the Service Tool, command unknown
- `2D`/`10` and `2D`/`11` (data format description according to the 3800S code) not tested

## Sources

- *stendec*, `RS_diehl.c` (2014): frame format, CRC, commands `2D`/`35` for the Platinum 3800S
- Diehl AKO, manual *PLATINUM-Service-Tool AnalyzeData* (version 1.02EN): cable, data types
- Measurements on a Platinum 4300 TL and captures of the PLATINUM Service Tool 2.48C (October 2026)
