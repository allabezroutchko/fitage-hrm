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
import ctypes
import os
import queue
import sys
import threading
import time
import traceback

import console
from objc_util import ObjCClass, ObjCInstance, create_objc_class, on_main_thread

import fitage_core as fc

SPORT = "boxing"            # default sport if no argument is given
CLEAR_AFTER_SYNC = False    # erase records on the strap after a clean sync
NAME_PREFIX = "FITAGE"
OUT_DIR = os.path.expanduser("~/Documents/FITAGE")   # "On My iPhone > Pythonista 3 > FITAGE"
SCAN_TIMEOUT = 20
IDLE_TIMEOUT = 15
DEBUG = False

# Pythonista's own `cb` module can't write to a characteristic on Pythonista
# 3.4 (SystemError "PY_SSIZE_T_CLEAN macro must be defined for '#' formats",
# github.com/omz/pythonista-issues/issues/699), so this script talks to
# Apple's CoreBluetooth directly through objc_util.
CBCentralManager = ObjCClass("CBCentralManager")
NSData = ObjCClass("NSData")
STATE_POWERED_ON = 5
WRITE_WITHOUT_RESPONSE = 1   # FC21 / FD0A only support write-without-response
BASE_SUFFIX = "-0000-1000-8000-00805F9B34FB"

STRAP = None   # the Strap instance the ObjC delegate forwards to


def short_uuid(u):
    u = str(u).upper()
    if len(u) == 36 and u.startswith("0000") and u.endswith(BASE_SUFFIX):
        return u[4:8]
    return u


def uuid_of(obj):
    return short_uuid(obj.UUID().UUIDString())


def to_bytes(nsdata):
    if nsdata is None:
        return None
    n = nsdata.length()
    return ctypes.string_at(nsdata.bytes(), n) if n else b""


def to_nsdata(data):
    data = bytes(data)
    buf = ctypes.create_string_buffer(data, len(data))
    return NSData.dataWithBytes_length_(buf, len(data))


def log(*a):
    if DEBUG:
        print("[debug]", *a)


def _call(name, *args):
    """Forward an ObjC delegate callback to STRAP.<name>; never let an
    exception escape into ObjC (that would crash Pythonista)."""
    try:
        if STRAP is not None:
            getattr(STRAP, name)(*[None if a is None else ObjCInstance(a) for a in args])
    except Exception:
        traceback.print_exc()


# --- ObjC delegate methods (CBCentralManagerDelegate + CBPeripheralDelegate) ---
def centralManagerDidUpdateState_(_self, _cmd, central):
    _call("on_state", central)


def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(_self, _cmd, central, p, adv, rssi):
    _call("on_discover", p)


def centralManager_didConnectPeripheral_(_self, _cmd, central, p):
    _call("on_connect", p)


def centralManager_didFailToConnectPeripheral_error_(_self, _cmd, central, p, error):
    _call("on_disconnect", p, error)


def centralManager_didDisconnectPeripheral_error_(_self, _cmd, central, p, error):
    _call("on_disconnect", p, error)


def peripheral_didDiscoverServices_(_self, _cmd, p, error):
    _call("on_services", p)


def peripheral_didDiscoverCharacteristicsForService_error_(_self, _cmd, p, service, error):
    _call("on_characteristics", service)


def peripheral_didUpdateValueForCharacteristic_error_(_self, _cmd, p, c, error):
    _call("on_value", c)


def make_delegate():
    # A fresh class name per run: re-running the script in the same Pythonista
    # session must not reuse a class bound to the previous run's functions.
    cls = create_objc_class(
        "FitageBLEDelegate%d" % int(time.time() * 1000),
        methods=[centralManagerDidUpdateState_,
                 centralManager_didDiscoverPeripheral_advertisementData_RSSI_,
                 centralManager_didConnectPeripheral_,
                 centralManager_didFailToConnectPeripheral_error_,
                 centralManager_didDisconnectPeripheral_error_,
                 peripheral_didDiscoverServices_,
                 peripheral_didDiscoverCharacteristicsForService_error_,
                 peripheral_didUpdateValueForCharacteristic_error_],
        protocols=["CBCentralManagerDelegate", "CBPeripheralDelegate"])
    return cls.alloc().init()


class Strap(object):
    def __init__(self):
        self.central = None
        self.delegate = None
        self.peripheral = None
        self.chars = {}
        self.powered = threading.Event()
        self.ready = threading.Event()
        self.disconnected = threading.Event()
        self.parser = fc.SyncParser(log=log)
        self.outq = queue.Queue()
        self.replies = queue.Queue()
        self.last_rx = time.time()

    # --- callbacks (run on the main thread) ---
    def on_state(self, central):
        state = central.state()
        log("central state", state)
        if state == STATE_POWERED_ON:
            self.powered.set()

    def on_discover(self, p):
        name = p.name()
        name = str(name) if name is not None else ""
        if self.peripheral is None and name.upper().startswith(NAME_PREFIX):
            print("Found %s, connecting..." % name)
            self.peripheral = p           # keep a reference, or iOS drops it
            p.setDelegate_(self.delegate)
            self.central.stopScan()
            self.central.connectPeripheral_options_(p, None)

    def on_connect(self, p):
        p.discoverServices_(None)

    def on_disconnect(self, p, error):
        log("disconnected", error)
        self.disconnected.set()

    def on_services(self, p):
        for s in p.services() or []:
            if uuid_of(s) in (fc.SVC_SYNC, fc.SVC_CMD):
                p.discoverCharacteristics_forService_(None, s)

    def on_characteristics(self, s):
        for c in s.characteristics() or []:
            self.chars[uuid_of(c)] = c
        if all(k in self.chars for k in (fc.SYNC_NOTIFY, fc.SYNC_WRITE, fc.CMD_NOTIFY, fc.CMD_WRITE)):
            self.ready.set()

    def on_value(self, c):
        data = to_bytes(c.value())
        if data is None:
            return
        u = uuid_of(c)
        if u == fc.SYNC_NOTIFY:
            self.last_rx = time.time()
            for r in self.parser.feed(data):
                self.outq.put(r)
        elif u == fc.CMD_NOTIFY:
            log("FD09 <-", data.hex())
            self.replies.put(data)

    # --- actions (called from the script thread) ---
    @on_main_thread
    def _start(self):
        self.delegate = make_delegate()
        self.central = CBCentralManager.alloc().initWithDelegate_queue_(self.delegate, None)

    def start(self):
        global STRAP
        STRAP = self
        self._start()
        return self.powered.wait(5)

    @on_main_thread
    def scan(self):
        self.central.scanForPeripheralsWithServices_options_(None, None)

    @on_main_thread
    def write(self, uuid, data):
        self.peripheral.writeValue_forCharacteristic_type_(
            to_nsdata(data), self.chars[uuid], WRITE_WITHOUT_RESPONSE)

    @on_main_thread
    def notify(self, uuid):
        self.peripheral.setNotifyValue_forCharacteristic_(True, self.chars[uuid])

    @on_main_thread
    def _stop(self):
        try:
            self.central.stopScan()
            if self.peripheral is not None:
                self.central.cancelPeripheralConnection_(self.peripheral)
        finally:
            if self.peripheral is not None:
                self.peripheral.setDelegate_(None)
            self.central.setDelegate_(None)

    def stop(self):
        global STRAP
        if self.central is not None:
            self._stop()
        STRAP = None

    def sync(self):
        self.notify(fc.CMD_NOTIFY)
        self.notify(fc.SYNC_NOTIFY)
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
    try:
        if not strap.start():
            print("Bluetooth is off, or Pythonista isn't allowed to use it "
                  "(Settings > Privacy > Bluetooth).")
            return
        print("Scanning for %s strap (wear it so it wakes up)..." % NAME_PREFIX)
        strap.scan()
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
        strap.stop()

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
