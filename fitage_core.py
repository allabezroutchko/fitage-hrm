"""
Shared, dependency-free core for the FITAGE / Onecoder heart-rate strap:
protocol constants, frame builders, the offline-record sync parser and a
minimal FIT (activity) file writer.

Used by fitage_hrm.py (Linux, bleak) and fitage_ios.py (Pythonista, cb).
Pure Python 3.8+, no third-party imports, so it runs inside Pythonista.
"""
import struct
import time

# ---------------------------------------------------------------- protocol --
# 16-bit UUIDs (Pythonista reports them like this; bleak uses the 128-bit form)
SVC_SYNC = "FC00"
SYNC_NOTIFY = "FC20"   # offline record stream (notify)
SYNC_WRITE = "FC21"    # offline record acks (write)
SVC_CMD = "FD00"
CMD_NOTIFY = "FD09"    # settings replies (notify)
CMD_WRITE = "FD0A"     # settings commands (write)
HR_MEASUREMENT = "2A37"

# Strap clock = UTC + standard (non-DST) offset, as Java's getRawOffset().
TZ_RAW_OFFSET = -time.timezone

CMD_ALARM_ON, CMD_ALARM_GET, CMD_ALARM_OFF = 0x09, 0x0A, 0x0B
CMD_MAXHR_SET, CMD_MAXHR_GET = 0x0C, 0x0D
CMD_CLEAR_RECORDS = 0x10
CMD_LIGHT_SET = 0x11


def csum(b):
    return sum(b) & 0xFF


def settings_frame(cmd, value=None):
    body = [0xB2, 4 if value is None else 5, cmd] + ([] if value is None else [value & 0xFF])
    return bytes(body + [csum(body)])


def ack_frame(seq):
    body = [0xE0, seq & 0xFF, 0]
    return bytes(body + [csum(body)])


def clock_frame(seq, now=None):
    t = int(time.time() if now is None else now) + TZ_RAW_OFFSET
    body = [0xE2, seq & 0xFF] + list(t.to_bytes(4, "big"))
    return bytes(body + [csum(body)])


def dev_to_unix(raw):
    return raw - TZ_RAW_OFFSET


class SyncParser:
    """Feed FC20 notifications in; get back frames to write to FC21.

    After `done` is True (or the strap goes quiet), `hr` holds
    (unix_time, bpm) samples and `rr` holds (unix_time, [rr_ms, ...]).
    """

    def __init__(self, log=None):
        self.log = log or (lambda *a: None)
        self._queue = []
        self._count = 0
        self._hr = {}
        self._rr = {}
        self.other = []
        self.warnings = []
        self.done = False

    def feed(self, d):
        d = bytes(d)
        self.log("FC20 <-", d.hex())
        if len(d) <= 2:
            return []
        if d[-1] != csum(d[:-1]):
            self.warnings.append("bad checksum, frame dropped: " + d.hex())
            return []
        seq, key, idx = d[0] >> 4, d[0] & 0x0F, d[1]
        payload = d[2:-1]
        # The strap re-sends frames/records it considers unacknowledged; it
        # expects a reply to every copy (FC21 is write-WITHOUT-response).
        # Duplicate records are harmless: samples are keyed by timestamp.
        replies = []
        if key == 2:
            replies.append(clock_frame(seq))     # strap asks for the time
        elif key in (1, 3, 7, 10):
            replies.append(ack_frame(seq))
        elif key in (4, 5, 9, 8, 12):
            last = 3 if key in (4, 5, 9) else 5
            if idx == 1:
                self._flush(key)
                self._queue.append(payload)
                self._count = 1
            elif idx == last:
                replies.append(ack_frame(seq))
                if self._count != d[2]:
                    self.warnings.append("record type %d: got %d frames, strap says %d" % (key, self._count, d[2]))
                self._flush(key)
                self._count = 0
            else:
                self._queue.append(payload)
                self._count += 1
        elif key == 6:
            replies.append(ack_frame(seq))
            self.done = True
        for r in replies:
            self.log("FC21 ->", r.hex())
        return replies

    @property
    def hr(self):
        return sorted(self._hr.items())

    @property
    def rr(self):
        return sorted(self._rr.items())

    def _flush(self, key):
        data = b"".join(self._queue)
        self._queue = []
        if not data:
            return
        if key == 8 and len(data) >= 4:
            t = dev_to_unix(int.from_bytes(data[:4], "big"))
            for i, bpm in enumerate(data[4:]):
                self._hr[t + 2 * i] = bpm
        elif key == 12 and len(data) >= 4:
            t = dev_to_unix(int.from_bytes(data[:4], "big"))
            body = data[4:]
            self._rr[t] = [int.from_bytes(body[i:i + 2], "big") for i in range(0, len(body) - 1, 2)]
        else:
            self.other.append((key, data))


def split_sessions(samples, max_gap=300, min_len=60):
    """Split sorted (t, bpm) samples into workouts at gaps > max_gap seconds;
    drop sessions shorter than min_len seconds and samples with bpm == 0."""
    samples = sorted((t, b) for t, b in samples if b > 0)
    sessions, cur = [], []
    for t, b in samples:
        if cur and t - cur[-1][0] > max_gap:
            sessions.append(cur)
            cur = []
        cur.append((t, b))
    if cur:
        sessions.append(cur)
    return [s for s in sessions if s[-1][0] - s[0][0] >= min_len]


# --------------------------------------------------------------- FIT writer --
FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z

# base types
ENUM, UINT8, UINT16, UINT32, UINT32Z = 0x00, 0x02, 0x84, 0x86, 0x8C
_FMT = {ENUM: "B", UINT8: "B", UINT16: "H", UINT32: "I", UINT32Z: "I"}

SPORTS = {  # name -> (sport, sub_sport) per FIT profile
    "generic": (0, 0),
    "running": (1, 0),
    "cycling": (2, 0),
    "indoor_cycling": (2, 6),
    "treadmill": (1, 1),
    "walking": (11, 0),
    "hiking": (17, 0),
    "training": (10, 0),
    "cardio": (10, 26),
    "strength": (10, 20),
    "hiit": (62, 0),
    "rowing": (15, 0),
    "boxing": (47, 0),
}

_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def fit_crc(data, crc=0):
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


class _FitEncoder:
    def __init__(self):
        self.buf = bytearray()
        self.defs = {}  # global msg -> (local id, field list)

    def msg(self, global_num, fields):
        """fields: list of (field_num, base_type, value)."""
        layout = tuple((n, bt) for n, bt, _ in fields)
        if global_num not in self.defs or self.defs[global_num][1] != layout:
            local = self.defs[global_num][0] if global_num in self.defs else len(self.defs)
            self.defs[global_num] = (local, layout)
            self.buf += struct.pack("<BBBHB", 0x40 | local, 0, 0, global_num, len(fields))
            for n, bt in layout:
                self.buf += struct.pack("<BBB", n, struct.calcsize(_FMT[bt]), bt)
        local = self.defs[global_num][0]
        self.buf.append(local)
        for _, bt, v in fields:
            self.buf += struct.pack("<" + _FMT[bt], v)

    def file_bytes(self):
        header = struct.pack("<BBHI4s", 12 + 2, 0x20, 2132, len(self.buf), b".FIT")
        header += struct.pack("<H", fit_crc(header))
        body = header + bytes(self.buf)
        return body + struct.pack("<H", fit_crc(body))


def build_fit(samples, sport="generic"):
    """samples: sorted list of (unix_time, bpm) for one workout. Returns bytes."""
    if not samples:
        raise ValueError("no samples")
    sp, sub = SPORTS[sport]
    ft = lambda t: int(t) - FIT_EPOCH
    t0, t1 = samples[0][0], samples[-1][0]
    elapsed_ms = int((t1 - t0) * 1000)
    bpms = [b for _, b in samples]
    avg, mx = round(sum(bpms) / len(bpms)), max(bpms)

    e = _FitEncoder()
    # file_id: type=activity, manufacturer=development(255)
    e.msg(0, [(0, ENUM, 4), (1, UINT16, 255), (2, UINT16, 1), (3, UINT32Z, 2003368), (4, UINT32, ft(t0))])
    # event: timer start
    e.msg(21, [(253, UINT32, ft(t0)), (0, ENUM, 0), (1, ENUM, 0)])
    for t, b in samples:
        e.msg(20, [(253, UINT32, ft(t)), (3, UINT8, b)])
    # event: timer stop_all
    e.msg(21, [(253, UINT32, ft(t1)), (0, ENUM, 0), (1, ENUM, 4)])
    # lap
    e.msg(19, [(253, UINT32, ft(t1)), (2, UINT32, ft(t0)), (7, UINT32, elapsed_ms), (8, UINT32, elapsed_ms),
               (15, UINT8, avg), (16, UINT8, mx), (0, ENUM, 9), (1, ENUM, 1), (25, ENUM, sp), (39, ENUM, sub)])
    # session
    e.msg(18, [(253, UINT32, ft(t1)), (2, UINT32, ft(t0)), (7, UINT32, elapsed_ms), (8, UINT32, elapsed_ms),
               (5, ENUM, sp), (6, ENUM, sub), (16, UINT8, avg), (17, UINT8, mx),
               (0, ENUM, 8), (1, ENUM, 1), (25, UINT16, 0), (26, UINT16, 1)])
    # activity
    e.msg(34, [(253, UINT32, ft(t1)), (0, UINT32, elapsed_ms), (1, UINT16, 1), (2, ENUM, 0),
               (3, ENUM, 26), (4, ENUM, 1), (5, UINT32, ft(t1) + time.localtime(t1).tm_gmtoff)])
    return e.file_bytes()


def fit_filename(samples):
    return time.strftime("fitage_%Y%m%d_%H%M.fit", time.localtime(samples[0][0]))
