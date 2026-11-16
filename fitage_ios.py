"""
Pythonista script: download offline-recorded heart rate from a FITAGE strap,
save each workout as a .fit file on the phone, and hand it to HealthFit.

Install: copy this file and fitage_core.py into the same Pythonista folder.
Run from a Shortcut ("Run Pythonista Script", Run in Pythonista = ON) or
directly. Optional argument: sport name, e.g. "cycling" (see SPORTS in
fitage_core.py). Default below.

Make sure no other device (PC/blueman, Heartool) is connected to the strap:
it accepts only one connection and stops advertising while connected.
"""
import os
import queue
import sys
import threading
import time

import cb
import console

import fitage_core as fc

SPORT = "cardio"            # default sport if no argument is given
CLEAR_AFTER_SYNC = False    # erase records on the strap after a clean sync
NAME_PREFIX = "FITAGE"
OUT_DIR = os.path.expanduser("~/Documents/FITAGE")   # "On My iPhone > Pythonista 3 > FITAGE"
SCAN_TIMEOUT = 20
IDLE_TIMEOUT = 15
DEBUG = False

BASE_SUFFIX = "-0000-1000-8000-00805F9B34FB"


def short_uuid(u):
    u = str(u).upper()
    if len(u) == 36 and u.startswith("0000") and u.endswith(BASE_SUFFIX):
        return u[4:8]
    return u


def log(*a):
    if DEBUG:
        print("[debug]", *a)


class Strap(object):
    def __init__(self):
        self.peripheral = None
        self.chars = {}
        self.ready = threading.Event()
        self.disconnected = threading.Event()
        self.parser = fc.SyncParser(log=log)
        self.outq = queue.Queue()
        self.replies = queue.Queue()
        self.last_rx = time.time()

    # --- cb central delegate ---
    def did_discover_peripheral(self, p):
        if self.peripheral is None and p.name and p.name.upper().startswith(NAME_PREFIX):
            print("Found %s, connecting..." % p.name)
            self.peripheral = p
            cb.stop_scan()
            cb.connect_peripheral(p)

    def did_connect_peripheral(self, p):
        p.discover_services()

    def did_fail_to_connect_peripheral(self, p, error):
        print("Connection failed:", error)
        self.disconnected.set()

    def did_disconnect_peripheral(self, p, error):
        log("disconnected", error)
        self.disconnected.set()

    def did_discover_services(self, p, error):
        for s in p.services:
            if short_uuid(s.uuid) in (fc.SVC_SYNC, fc.SVC_CMD):
                p.discover_characteristics(s)

    def did_discover_characteristics(self, s, error):
        for c in s.characteristics:
            self.chars[short_uuid(c.uuid)] = c
        if all(k in self.chars for k in (fc.SYNC_NOTIFY, fc.SYNC_WRITE, fc.CMD_NOTIFY, fc.CMD_WRITE)):
            self.ready.set()

    def did_update_value(self, c, error):
        u = short_uuid(c.uuid)
        if c.value is None:
            return
        if u == fc.SYNC_NOTIFY:
            self.last_rx = time.time()
            for r in self.parser.feed(c.value):
                self.outq.put(r)
        elif u == fc.CMD_NOTIFY:
            log("FD09 <-", bytes(c.value).hex())
            self.replies.put(bytes(c.value))

    def did_write_value(self, c, error):
        if error:
            print("write error:", error)

    # --- actions (main thread) ---
    def write(self, uuid, data):
        # FC21 / FD0A only support write-without-response
        self.peripheral.write_characteristic_value(self.chars[uuid], data, False)

    def sync(self):
        p = self.peripheral
        p.set_notify_value(self.chars[fc.CMD_NOTIFY], True)
        p.set_notify_value(self.chars[fc.SYNC_NOTIFY], True)
        self.last_rx = time.time()
        while not self.parser.done and not self.disconnected.is_set():
            try:
                self.write(fc.SYNC_WRITE, self.outq.get(timeout=0.5))
            except queue.Empty:
                if time.time() - self.last_rx > IDLE_TIMEOUT:
                    print("Strap went quiet before signalling end of sync.")
                    break
        time.sleep(0.3)
        while not self.outq.empty():   # final ack
            self.write(fc.SYNC_WRITE, self.outq.get_nowait())
        time.sleep(0.3)

    def clear_records(self):
        frame = fc.settings_frame(fc.CMD_CLEAR_RECORDS, 1)
        self.write(fc.CMD_WRITE, frame)
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                r = self.replies.get(timeout=0.3)
            except queue.Empty:
                continue
            if len(r) >= 3 and r[0] == 0xD2 and r[2] == fc.CMD_CLEAR_RECORDS:
                return True
        return False


def main():
    sport = sys.argv[1].strip().lower() if len(sys.argv) > 1 and sys.argv[1].strip() else SPORT
    if sport not in fc.SPORTS:
        print("Unknown sport %r, using %s. Options: %s" % (sport, SPORT, ", ".join(sorted(fc.SPORTS))))
        sport = SPORT

    strap = Strap()
    cb.set_central_delegate(strap)
    print("Scanning for %s strap (wear it so it wakes up)..." % NAME_PREFIX)
    cb.scan_for_peripherals()
    try:
        if not strap.ready.wait(SCAN_TIMEOUT):
            console.hud_alert("Strap not found", "error", 2)
            print("Strap not found / not connectable. Is it worn, and not connected to another device?")
            return
        print("Connected. Syncing...")
        strap.sync()
        p = strap.parser
        for w in p.warnings:
            print("warning:", w)
        if not p.done:
            print("Sync incomplete; saving what was received.")
        if CLEAR_AFTER_SYNC:
            if p.done and p.hr:
                print("Clearing strap records:", "OK" if strap.clear_records() else "no confirmation")
            else:
                print("Not clearing (sync incomplete or empty).")
    finally:
        try:
            if strap.peripheral is not None:
                cb.cancel_peripheral_connection(strap.peripheral)
        finally:
            cb.reset()

    sessions = fc.split_sessions(p.hr)
    print("%d HR samples -> %d workout(s)" % (len(p.hr), len(sessions)))
    if not sessions:
        console.hud_alert("No stored workouts", "success", 2)
        return
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    paths = []
    for s in sessions:
        path = os.path.join(OUT_DIR, fc.fit_filename(s))
        with open(path, "wb") as f:
            f.write(fc.build_fit(s, sport))
        bpm = [b for _, b in s]
        print("  %s  %d min  avg %d  max %d" % (os.path.basename(path), (s[-1][0] - s[0][0]) // 60,
                                                 round(sum(bpm) / len(bpm)), max(bpm)))
        paths.append(path)
    for path in paths:
        # Share sheet: choose HealthFit (or "Copy to HealthFit").
        console.open_in(path)


if __name__ == "__main__":
    main()
