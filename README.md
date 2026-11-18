# fitage-hrm

Use a FITAGE heart rate chest strap ([fitage.nl](https://fitage.nl)) without the
Heartool app, its online account, or its cloud upload.

The strap's extra features normally require Heartool. These scripts talk to the
strap directly over Bluetooth Low Energy instead:

- set the over-limit **alarm** (the strap beeps while your HR is above a threshold)
- set the stored **max HR** (used by the strap to calculate the heart rate zones to change the LED color depending on the heart rate zone you are in)
- **download the heart rate recorded while offline** to CSV and `.fit` workout files
- read battery and firmware info, stream live HR

There are two front ends sharing one core:

| File | Runs on | Purpose |
|---|---|---|
| `fitage_core.py` | anywhere (pure Python, no dependencies) | protocol, offline-sync parser, FIT file writer |
| `fitage_hrm.py` | Linux (BlueZ + [bleak](https://github.com/hbldh/bleak)) | command-line tool for every feature |
| `fitage_ios.py` | iPhone/iPad ([Pythonista 3](http://omz-software.com/pythonista/)) | one-tap sync → `.fit` → HealthFit / Apple Health |

Tested with a strap named `FITAGE-2003368`, firmware V1.1.2, hardware V1.0.0.
The strap is made by Onecoder (Shenzhen), so other Onecoder heart rate straps
using the same "FitBleKit" SDK may work too. It seems that Fitage HRM has the 
same chipset and features as Coospo H9Z, so the scripts may work for that one as well. 

## Linux

### Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

You need a Bluetooth adapter. Wear the strap: it only wakes up with skin contact.

### Usage on Linux

```sh
.venv/bin/python fitage_hrm.py scan                     # find the strap's address
A=CE:42:4F:EE:DE:A8                                     # your strap

.venv/bin/python fitage_hrm.py -a $A info               # battery, firmware, max HR, alarm
.venv/bin/python fitage_hrm.py -a $A set-max-hr 185
.venv/bin/python fitage_hrm.py -a $A alarm on 160       # beep while HR > 160 bpm
.venv/bin/python fitage_hrm.py -a $A alarm on --pct 90  # 90% of the stored max HR
.venv/bin/python fitage_hrm.py -a $A alarm off
.venv/bin/python fitage_hrm.py -a $A live               # stream live HR (Ctrl-C to stop)
.venv/bin/python fitage_hrm.py -a $A sync --fit --sport running
```

`sync` writes `hrm_<timestamp>_hr.csv` (one row per 2-second sample). With
`--fit` it also writes one `fitage_YYYYMMDD_HHMM.fit` workout per recording.
Recordings are split at gaps of more than 5 minutes. Sport choices are
`cardio` (the default), `running`, `cycling`, `indoor_cycling`, `treadmill`,
`walking`, `hiking`, `training`, `strength`, `hiit`, `rowing`, `boxing` and `generic`.

Other options:

- `-d` hex-dumps all Bluetooth traffic.
- `alarm-test` sets a threshold, streams HR, and lets you mark beeps with Enter.
  It restores your settings on Ctrl-C. It was used to work out how the alarm
  behaves.
- `--adapter hci1` selects another BlueZ adapter.

### Notes

- **The strap accepts only one connection** and stops advertising while
  connected. If blueman or another desktop tool auto-connects it, `scan` won't
  see it. Pass the address with `-a` instead: the script then uses BlueZ's
  existing connection. To let the strap record offline, stop the PC holding it:
  ```sh
  bluetoothctl disconnect $A
  bluetoothctl untrust $A        # stop automatic reconnection
  ```
- The strap **only records while nothing is connected to it**.

## iPhone: sync to HealthFit / Apple Health

`fitage_ios.py` syncs the strap from your phone, saves each workout as a `.fit`
file, and opens the share sheet so you can import it into
[HealthFit](https://apps.apple.com/app/healthfit/id1202650514), which writes it
to Apple Health.

1. Copy `fitage_core.py` and `fitage_ios.py` into the same folder in Pythonista.
2. Create a Shortcut with the action **Pythonista → Run Script**:
   - Script: `fitage_ios.py`
   - **Run in Pythonista: ON** (Bluetooth and the share sheet need the app)
   - Optional argument: a sport name, e.g. `cycling` (default `boxing`)
3. Make sure nothing else (the PC, Heartool) is connected to the strap. Run the
   Shortcut, then choose **HealthFit** in the share sheet.

Files are kept in *On My iPhone → Pythonista 3 → FITAGE*. You can change the
settings at the top of the script: `SPORT`, `NAME_PREFIX`, `OUT_DIR`, `DEBUG`.

> The script calls Apple's CoreBluetooth directly through `objc_util`.
> Pythonista's own `cb` module can't write to a characteristic on
> Pythonista 3.4: every write fails with `SystemError: PY_SSIZE_T_CLEAN macro
> must be defined for '#' formats`
> ([pythonista-issues#699](https://github.com/omz/pythonista-issues/issues/699)).
> If something fails, set `DEBUG = True` and look at the console output.

## ⚠️ Syncing deletes the data from the strap

After a completed sync **the strap erases its stored records**. Each sync
returns only what was recorded since the previous one. Keep the CSV/FIT files
it produces: they are the only copy.

## Protocol

Reverse engineered by decompiling Heartool 2.0.5 for Android
(`cn.fitcare.heartool`). The app is written in Flutter, but its Bluetooth layer
is Onecoder's Java "FitBleKit" SDK, which decompiles cleanly with
[jadx](https://github.com/skylot/jadx). The strap uses the SDK's `BleHeartRate`
device type with its "OldTracker" protocol. The behaviour below was checked
against a real strap.

### GATT

| Service | Characteristic | Properties | Use |
|---|---|---|---|
| `180D` | `2A37` | notify | standard live heart rate |
| `180F` | `2A19` | read | battery % |
| `FD00` | `FD0A` | write without response | settings commands |
| `FD00` | `FD09` | notify | settings replies |
| `FC00` | `FC20` | notify | offline record stream |
| `FC00` | `FC21` | write without response | offline sync replies |

`FC21` and `FD0A` **only accept write-without-response**. If the sync replies
are sent as writes with response, the strap ignores them and retransmits every
record about 4 times.

### Settings commands (`FD0A` → reply on `FD09`)

Frame format: `B2 <total length> <cmd> [value] <checksum>`. The checksum is
the sum of the preceding bytes, keeping the low 8 bits. The reply is
`D2 <len> <cmd> …`.

| Cmd | Frame | Meaning |
|---|---|---|
| `09` | `B2 05 09 <bpm> cs` | alarm on: beep about once a second while HR > `bpm` |
| `0A` | `B2 04 0A C0` | read alarm → `D2 06 0A <on> <bpm> cs` |
| `0B` | `B2 04 0B C1` | alarm off |
| `0C` | `B2 05 0C <bpm> cs` | set max HR |
| `0D` | `B2 04 0D C3` | read max HR → `D2 05 0D <bpm> cs` |
| `10` | `B2 05 10 01 C8` | clear stored records |
| `11` | `B2 05 11 <0/1> cs` | LED on/off |
| `12` | `B2 04 12 C8` | read LED → `D2 05 12 <0/1> cs` |

How the alarm behaves, measured with `alarm-test`:

- The threshold is in **absolute bpm**. The firmware raises values below 30 to 30.
- The alarm stops within about 1 second of HR dropping back to the threshold.
- It keeps working with nothing connected.
- **Max HR does not affect the alarm.** Heartool's "beep above 90% of max HR"
  is presumably the app computing the bpm value; `alarm on --pct 90` does the
  same.
- **The alarm takes over the buzzer.** With the alarm off, the strap beeps
  once when it picks up a heartbeat and three times when it goes to sleep.
  With the alarm on, those beeps are gone and only the over-threshold beeping
  remains. The LED flashes at startup either way. You get the status beeps or
  the alarm, not both.

### Offline sync (`FC20` notify, replies on `FC21`)

Enabling notifications on `FC20` starts the sync. Each frame is
`<seq<<4 | type> <index> <payload…> <checksum>`. The host must reply to
**every** frame it is expected to acknowledge, including repeated copies, with
`E0 <seq> 00 cs`:

| Type | Content | Reply |
|---|---|---|
| 1 | hello: strap MAC address (byte-reversed) and device info | ack |
| 2 | the strap's clock (seconds) | `E2 <seq> <local time, 4 bytes big-endian> cs` sets the clock |
| 8 | HR record. Frames with index 1–4 carry the data; the frame with index 5 ends the record and gives the frame count. Data is a 4-byte start time followed by one HR byte every 2 seconds, up to about 2 minutes per record | ack on the end frame |
| 12 | RR intervals: start time, then 16-bit big-endian values | ack on the end frame |
| 6 | end of sync, payload ASCII `"done"` | ack |

Timestamps are "local" seconds: Unix time plus the standard (non-DST) UTC
offset, as in Java's `TimeZone.getRawOffset()`.

## Disclaimer

This is an independent interoperability project. It is not affiliated with
FITAGE, Onecoder or the Heartool app. It is not a medical device: don't rely on
it for anything health-critical.
