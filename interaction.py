"""Physical button gestures and magnetometer calibration, independent of Qt/USB."""

import json
import math
from pathlib import Path


class ButtonTracker:
    def __init__(self, long_seconds=0.8):
        self.long_seconds = long_seconds
        self.previous = None
        self.pressed_at = None
        self.presses = 0

    def update(self, code, status, timestamp):
        current = (code, status)
        if current == self.previous:
            return None
        previous = self.previous
        self.previous = current
        if previous is None or code != 0x71 or status not in (0, 1):
            return None
        if status == 1:
            self.pressed_at = timestamp
            self.presses += 1
            return {
                "phase": "press",
                "code": code,
                "status": status,
                "presses": self.presses,
            }
        if self.pressed_at is None:
            return None
        duration = max(0.0, timestamp - self.pressed_at)
        self.pressed_at = None
        return {
            "phase": "release",
            "gesture": "long" if duration >= self.long_seconds else "short",
            "duration_ms": round(duration * 1000),
            "code": code,
            "status": status,
            "presses": self.presses,
        }


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def unit(vector):
    length = math.sqrt(dot(vector, vector))
    return [value / length for value in vector] if length > 1e-9 else None


def magnetic_heading(magnetic, gravity, forward=(0.0, 0.0, 1.0)):
    """Clockwise magnetic heading in device coordinates (+Y down, +Z forward).

    Project the magnetic field and device forward axis onto the plane normal to
    gravity. No declination or verified board-to-head alignment is implied.
    """
    down = unit(gravity)
    if down is None or not any(magnetic):
        return None
    projected = [value - dot(forward, down) * g for value, g in zip(forward, down)]
    facing = unit(projected)
    if facing is None:
        return None
    right = [
        down[1] * facing[2] - down[2] * facing[1],
        down[2] * facing[0] - down[0] * facing[2],
        down[0] * facing[1] - down[1] * facing[0],
    ]
    north, east = dot(magnetic, facing), -dot(magnetic, right)
    if math.hypot(north, east) < 1e-6:
        return None
    return math.degrees(math.atan2(east, north)) % 360


class CompassCalibration:
    def __init__(self):
        self.offset = [0.0, 0.0, 0.0]
        self.scale = [1.0, 1.0, 1.0]
        self.valid = False
        self.collecting = False
        self.samples = 0
        self.minimum = [math.inf] * 3
        self.maximum = [-math.inf] * 3
        self.zero = 0.0

    def start(self):
        self.collecting = True
        self.samples = 0
        self.minimum = [math.inf] * 3
        self.maximum = [-math.inf] * 3

    def add(self, values):
        if self.collecting and any(values):
            self.samples += 1
            for i, value in enumerate(values):
                self.minimum[i] = min(self.minimum[i], value)
                self.maximum[i] = max(self.maximum[i], value)

    def spans(self):
        return (
            [high - low for low, high in zip(self.minimum, self.maximum)]
            if self.samples
            else [0.0, 0.0, 0.0]
        )

    def finish(self):
        spans = self.spans()
        if self.samples < 60 or min(spans) < 40 or min(spans) < max(spans) * 0.2:
            raise ValueError(
                "校准覆盖不足：请继续缓慢转动眼镜，让三个轴都经过多个方向。"
            )
        self.offset = [
            (low + high) / 2 for low, high in zip(self.minimum, self.maximum)
        ]
        mean = sum(spans) / 3
        self.scale = [mean / span for span in spans]
        self.valid = True
        self.collecting = False
        self.zero = 0.0

    def correct(self, values):
        return [
            (value - offset) * scale
            for value, offset, scale in zip(values, self.offset, self.scale)
        ]

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "offset": self.offset,
                    "scale": self.scale,
                    "valid": self.valid,
                    "zero": self.zero,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def load(self, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            offset, scale = data["offset"], data["scale"]
            if (
                len(offset) != 3
                or len(scale) != 3
                or not all(math.isfinite(v) for v in offset + scale)
            ):
                return
            if min(scale) <= 0:
                return
            self.offset, self.scale = offset, scale
            self.valid = bool(data.get("valid"))
            self.zero = float(data.get("zero", 0)) % 360
        except (OSError, ValueError, KeyError, TypeError):
            pass
