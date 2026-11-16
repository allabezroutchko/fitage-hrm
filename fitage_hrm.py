#!/usr/bin/env python3
"""
Talk to a Fitage / Onecoder ("FitBleKit" BleHeartRate) chest strap directly,
without the Heartool app or an account.

Protocol reverse engineered from Heartool 2.0.5 (cn.fitcare.heartool), which
embeds Onecoder's FitBleKit SDK:
  HRChannel.kt                 -> FBKApiHeartRate
  FBKProtocolOldTracker        -> which characteristic each command goes to
  FBKArmBandCmdList            -> settings command bytes   (B2 ...)
  FBKOldCommand                -> sync ack / clock bytes   (E0 / E2 ...)
  FBKOldAnalytical             -> offline record parsing   (FC20 notifications)

Settings frame (write to FD0A, reply notified on FD09 as D2 xx <cmd> ...):
    B2 <total_len> <cmd> [value] <checksum = sum(bytes) & 0xFF>
      cmd 0x09 alarm on at <bpm> ("setShock")        0x0A get alarm -> <on> <bpm>
      cmd 0x0B alarm off ("closeShock")
      cmd 0x0C set max HR ("setMaxInterval")         0x0D get max HR
    Verified on FITAGE fw V1.1.2: the alarm beeps ~1/s while HR > <bpm>
    (firmware clamps the threshold to >= 30); it runs on the strap with no
    host connected, and stops within ~1 s of HR dropping back to/below <bpm>
    (no noticeable hysteresis). Max HR does not affect the alarm.
      cmd 0x10 clear stored records (value 01)
      cmd 0x11 light switch (00/01)                   0x12 get light

Offline sync: enable notifications on FC20; the strap streams frames
    <seq<<4 | key> <frame_idx> <payload...> <checksum>
and the host acknowledges on FC21 with E0 <seq> 00 <cs> (or E2 <seq> <time32> <cs>
to set the clock when key == 2). key 8 = HR record (4-byte local-time timestamp,
then one HR byte every 2 s), key 12 = RR intervals, key 6 = sync finished.
"""
import argparse
import asyncio
import csv
import datetime as dt
import sys
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

import fitage_core as fc


def uuid16(x: int) -> str:
    return f"0000{x:04x}-0000-1000-8000-00805f9b34fb"


HR_SERVICE = uuid16(0x180D)
HR_MEASUREMENT = uuid16(0x2A37)
BATTERY_LEVEL = uuid16(0x2A19)
FW_REVISION = uuid16(0x2A26)
HW_REVISION = uuid16(0x2A27)
CMD_WRITE = uuid16(0xFD0A)    # settings commands
CMD_NOTIFY = uuid16(0xFD09)   # settings replies
SYNC_NOTIFY = uuid16(0xFC20)  # offline record stream
SYNC_WRITE = uuid16(0xFC21)   # offline record acks

# Device stores "local" seconds = UTC + standard (non-DST) offset, like Java's
# TimeZone.getRawOffset() used by the SDK.
TZ_RAW_OFFSET = -time.timezone

DEBUG = False


def log(*a):
    if DEBUG:
        print("[debug]", *a, file=sys.stderr)


def csum(b) -> int:
    return sum(b) & 0xFF


def settings_frame(cmd: int, value: int | None = None) -> bytes:
    body = [0xB2, 4 if value is None else 5, cmd] + ([] if value is None else [value & 0xFF])
    return bytes(body + [csum(body)])


async def find_address(name_hint: str | None) -> str:
    print("Scanning for heart-rate straps (8 s)...", file=sys.stderr)
    found = await BleakScanner.discover(timeout=8.0, return_adv=True)
    hits = []
    for dev, adv in found.values():
        name = adv.local_name or dev.name or ""
        is_hr = HR_SERVICE in [u.lower() for u in adv.service_uuids]
        if (name_hint and name_hint.lower() in name.lower()) or (not name_hint and is_hr):
            hits.append((dev.address, name, adv.rssi))
    if not hits:
        sys.exit("No strap found. Wear it (it needs skin contact to wake) and retry, or pass --address.")
    for a, n, r in hits:
        print(f"  {a}  {n!r}  rssi={r}", file=sys.stderr)
    if len(hits) > 1:
        sys.exit("Several devices found; pick one with --address.")
    return hits[0][0]


class Strap:
    def __init__(self, client: BleakClient):
        self.c = client
        self.replies: asyncio.Queue[bytes] = asyncio.Queue()

    async def start(self):
        await self.c.start_notify(CMD_NOTIFY, lambda _, d: self._on_reply(bytes(d)))

    def _on_reply(self, d: bytes):
        log("FD09 <-", d.hex(" "))
        self.replies.put_nowait(d)

    async def command(self, cmd: int, value: int | None = None, timeout=3.0) -> bytes | None:
        frame = settings_frame(cmd, value)
        log("FD0A ->", frame.hex(" "))
        while not self.replies.empty():
            self.replies.get_nowait()
        await self.c.write_gatt_char(CMD_WRITE, frame, response=False)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                return None
            try:
                r = await asyncio.wait_for(self.replies.get(), left)
            except asyncio.TimeoutError:
                return None
            if len(r) >= 3 and r[0] == 0xD2 and r[2] == cmd:
                return r
            log("ignoring unrelated reply", r.hex(" "))

    async def read_str(self, uuid):
        try:
            return (await self.c.read_gatt_char(uuid)).decode(errors="replace").strip("\x00 ")
        except Exception:
            return None


async def cmd_info(s: Strap):
    try:
        batt = (await s.c.read_gatt_char(BATTERY_LEVEL))[0]
    except Exception:
        batt = None
    print(f"battery:      {batt if batt is not None else '?'} %")
    print(f"firmware:     {await s.read_str(FW_REVISION)}")
    print(f"hardware:     {await s.read_str(HW_REVISION)}")
    r = await s.command(0x0D)
    print(f"max HR:       {r[3] if r and len(r) > 3 else '? (no reply)'}" + (f"   raw {r.hex(' ')}" if r else ""))
    r = await s.command(0x0A)
    if r and len(r) > 4:
        print(f"alarm:        {'ON, beeps above ' + str(r[4]) + ' bpm' if r[3] else 'off'}   raw {r.hex(' ')}")
    else:
        print("alarm:        ? (no reply)")


async def cmd_simple(s: Strap, cmd: int, value: int | None, what: str):
    r = await s.command(cmd, value)
    print(f"{what}: {'OK' if r else 'no confirmation from strap'}" + (f"  (reply {r.hex(' ')})" if r else ""))


async def cmd_sync(s: Strap, out_prefix: str, clear: bool, idle_timeout: float,
                   fit: bool = False, sport: str = "cardio"):
    st = fc.SyncParser(log=log)
    outq: asyncio.Queue[bytes] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    last_rx = [loop.time()]

    def on_frame(_, d: bytearray):
        last_rx[0] = loop.time()
        for r in st.feed(d):
            outq.put_nowait(r)

    print("Starting sync...", file=sys.stderr)
    await s.c.start_notify(SYNC_NOTIFY, on_frame)
    while not st.done:
        try:
            await s.c.write_gatt_char(SYNC_WRITE, await asyncio.wait_for(outq.get(), 0.5), response=False)
        except asyncio.TimeoutError:
            if loop.time() - last_rx[0] > idle_timeout:
                print("Strap went quiet before signalling end of sync.", file=sys.stderr)
                break
    while not outq.empty():  # send the final ack
        await s.c.write_gatt_char(SYNC_WRITE, outq.get_nowait(), response=False)
    await s.c.stop_notify(SYNC_NOTIFY)
    for w in st.warnings:
        print("warning:", w, file=sys.stderr)

    loc = lambda t: dt.datetime.fromtimestamp(t).astimezone()
    if st.hr:
        path = f"{out_prefix}_hr.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "unix", "bpm"])
            for t, bpm in st.hr:
                w.writerow([loc(t).isoformat(timespec="seconds"), t, bpm])
        print(f"{len(st.hr)} HR samples ({loc(st.hr[0][0]):%Y-%m-%d %H:%M} .. {loc(st.hr[-1][0]):%Y-%m-%d %H:%M}) -> {path}")
    else:
        print("No stored HR samples.")
    if st.rr:
        path = f"{out_prefix}_rr.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["block_time", "rr_ms"])
            for t, rrs in st.rr:
                for v in rrs:
                    w.writerow([loc(t).isoformat(timespec="seconds"), v])
        print(f"{sum(len(r) for _, r in st.rr)} RR intervals -> {path}")
    for key, data in st.other:
        print(f"(unparsed record type {key}, {len(data)} bytes: {data[:32].hex(' ')}...)")
    if fit:
        for sess in fc.split_sessions(st.hr):
            path = fc.fit_filename(sess)
            with open(path, "wb") as f:
                f.write(fc.build_fit(sess, sport))
            print(f"workout {loc(sess[0][0]):%Y-%m-%d %H:%M}, {(sess[-1][0] - sess[0][0]) // 60} min -> {path}")

    if clear:
        if st.done and (st.hr or st.rr):
            await cmd_simple(s, 0x10, 1, "clear stored records")
        elif st.done:
            print("Nothing synced; not clearing.")
        else:
            print("Sync did not finish cleanly; NOT clearing records.")


async def cmd_live(s: Strap):
    def on_hr(_, d: bytearray):
        flags = d[0]
        i = 1
        if flags & 0x01:
            bpm = int.from_bytes(d[i:i + 2], "little"); i += 2
        else:
            bpm = d[i]; i += 1
        if flags & 0x08:
            i += 2  # energy expended
        rr = []
        if flags & 0x10:
            while i + 1 < len(d):
                rr.append(round(int.from_bytes(d[i:i + 2], "little") * 1000 / 1024)); i += 2
        print(f"{dt.datetime.now():%H:%M:%S}  {bpm:3d} bpm" + (f"  RR {rr} ms" if rr else ""), flush=True)

    await s.c.start_notify(HR_MEASUREMENT, on_hr)
    print("Streaming heart rate, Ctrl-C to stop.", file=sys.stderr)
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass


async def cmd_alarm_test(s: Strap, max_hr: int, value: int):
    def parse(r, n):
        return list(r[3:3 + n]) if r and len(r) >= 3 + n + 1 else None

    orig_max = parse(await s.command(0x0D), 1)
    orig_alarm = parse(await s.command(0x0A), 2)
    print(f"original: max HR={orig_max}, alarm [switch, value]={orig_alarm}")
    try:
        await cmd_simple(s, 0x0C, max_hr, f"set max HR {max_hr}")
        await cmd_simple(s, 0x09, value, f"alarm on (value {value})")
        print(f"now:      max HR={parse(await s.command(0x0D), 1)}, "
              f"alarm [switch, value]={parse(await s.command(0x0A), 2)}")
        print(f"for reference: 90% of {max_hr} = {round(max_hr * 0.9)}")

        history: list[tuple[float, int]] = []
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        def on_hr(_, d: bytearray):
            bpm = int.from_bytes(d[1:3], "little") if d[0] & 1 else d[1]
            history.append((loop.time(), bpm))
            print(f"{loop.time() - t0:7.1f}s  {bpm:3d} bpm", flush=True)

        def on_enter():
            sys.stdin.readline()
            now = loop.time()
            recent = [b for t, b in history if now - t <= 5]
            cur = history[-1][1] if history else None
            print(f">>> BEEP marked at {now - t0:.1f}s: HR now {cur}, last 5 s {recent}", flush=True)

        await s.c.start_notify(HR_MEASUREMENT, on_hr)
        loop.add_reader(sys.stdin, on_enter)
        print("Streaming HR. Press Enter whenever the strap beeps; Ctrl-C to finish and restore settings.")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            loop.remove_reader(sys.stdin)
            try:
                await s.c.stop_notify(HR_MEASUREMENT)
            except Exception:
                pass
    finally:
        print("restoring original settings...")
        if orig_max:
            await cmd_simple(s, 0x0C, orig_max[0], f"max HR back to {orig_max[0]}")
        if orig_alarm and orig_alarm[0]:
            await cmd_simple(s, 0x09, orig_alarm[1], f"alarm back on (value {orig_alarm[1]})")
        else:
            await cmd_simple(s, 0x0B, None, "alarm off")


async def main():
    global DEBUG
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("-a", "--address", help="strap MAC address (otherwise scan)")
    p.add_argument("-n", "--name", help="pick device whose name contains this when scanning")
    p.add_argument("--adapter", default="hci0", help="BlueZ adapter (default hci0)")
    p.add_argument("-d", "--debug", action="store_true", help="hex-dump all traffic")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="list nearby heart-rate straps")
    sub.add_parser("info", help="battery, versions, current max HR and alarm settings")
    m = sub.add_parser("set-max-hr", help="store your maximum heart rate on the strap")
    m.add_argument("bpm", type=int)
    al = sub.add_parser("alarm", help="turn the over-limit alarm on/off")
    al.add_argument("state", choices=["on", "off"])
    al.add_argument("bpm", type=int, nargs="?", default=None,
                    help="beep while heart rate is above this many bpm (min 30)")
    al.add_argument("--pct", type=int, default=None,
                    help="instead of bpm: threshold as %% of the max HR stored on the strap (app uses 90)")
    lg = sub.add_parser("light", help="turn the LED on/off")
    lg.add_argument("state", choices=["on", "off"])
    sy = sub.add_parser("sync", help="download offline-stored HR data to CSV")
    sy.add_argument("-o", "--out", default=f"hrm_{dt.datetime.now():%Y%m%d_%H%M%S}",
                    help="output file prefix (default hrm_<timestamp>)")
    sy.add_argument("--clear", action="store_true", help="erase records on the strap after a clean sync")
    sy.add_argument("--idle-timeout", type=float, default=15.0)
    sy.add_argument("--fit", action="store_true", help="also write one .fit workout file per recording")
    sy.add_argument("--sport", default="cardio", choices=sorted(fc.SPORTS), help="sport for .fit files")
    sub.add_parser("live", help="print live heart rate")
    at = sub.add_parser("alarm-test", help="set max HR + alarm, stream HR, mark beeps with Enter, restore on Ctrl-C")
    at.add_argument("--max", type=int, default=120, help="max HR to set during the test (default 120)")
    at.add_argument("--value", type=int, default=1, help="alarm (setShock) value (default 1)")
    args = p.parse_args()
    DEBUG = args.debug

    if args.cmd == "scan":
        await find_address(args.name)
        return

    if args.cmd == "set-max-hr" and not 60 <= args.bpm <= 250:
        sys.exit("max HR must be between 60 and 250 bpm")
    if args.cmd == "alarm" and args.state == "on":
        if (args.bpm is None) == (args.pct is None):
            sys.exit("alarm on needs either a bpm value or --pct")
        if args.bpm is not None and not 30 <= args.bpm <= 250:
            sys.exit("alarm threshold must be 30..250 bpm")

    addr = args.address or await find_address(args.name)
    print(f"Connecting to {addr}...", file=sys.stderr)
    # A strap that is already connected (e.g. by blueman/BlueZ auto-connect)
    # stops advertising, so bleak's scan-by-address can't find it. Point bleak
    # straight at BlueZ's object for the device instead; that works whether it
    # is connected or not, as long as BlueZ knows it (paired / seen before).
    path = f"/org/bluez/{args.adapter}/dev_{addr.upper().replace(':', '_')}"
    target = BLEDevice(addr.upper(), None, {"path": path})
    async with BleakClient(target, timeout=20.0) as client:
        s = Strap(client)
        await s.start()
        if args.cmd == "info":
            await cmd_info(s)
        elif args.cmd == "set-max-hr":
            await cmd_simple(s, 0x0C, args.bpm, f"set max HR to {args.bpm}")
        elif args.cmd == "alarm":
            if args.state == "on":
                bpm = args.bpm
                if args.pct is not None:
                    r = await s.command(0x0D)
                    if not r or len(r) < 5:
                        sys.exit("could not read max HR from strap")
                    bpm = round(r[3] * args.pct / 100)
                    print(f"{args.pct}% of max HR {r[3]} = {bpm} bpm")
                    if not 30 <= bpm <= 250:
                        sys.exit("resulting threshold out of range")
                await cmd_simple(s, 0x09, bpm, f"alarm on above {bpm} bpm")
            else:
                await cmd_simple(s, 0x0B, None, "alarm off")
        elif args.cmd == "light":
            await cmd_simple(s, 0x11, 1 if args.state == "on" else 0, f"light {args.state}")
        elif args.cmd == "sync":
            await cmd_sync(s, args.out, args.clear, args.idle_timeout, args.fit, args.sport)
        elif args.cmd == "live":
            await cmd_live(s)
        elif args.cmd == "alarm-test":
            await cmd_alarm_test(s, args.max, args.value)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
