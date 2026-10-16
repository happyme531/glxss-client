"""G25 USB protocol recovered from the official SDK, with a small standalone CLI."""

from __future__ import annotations

import argparse
import json
import math
import struct
import subprocess
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import usb.core
import usb.util
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
VID, PID = 0x2E09, 0x0030
MODES = {
    "640x480": (4, 640, 480),
    "1280x720": (3, 1280, 720),
    "1920x1080": (1, 1920, 1080),
}
LUMA_LUT = [max(0, min(255, round((i - 16) * 255 / 219))) for i in range(256)]
CHROMA_LUT = [max(0, min(255, round((i - 128) * 255 / 224 + 128))) for i in range(256)]


def backend():
    try:
        import libusb_package

        return libusb_package.get_libusb1_backend()
    except ImportError:
        import usb.backend.libusb1

        return usb.backend.libusb1.get_backend()


def find_device(vid=VID, pid=PID):
    return usb.core.find(idVendor=vid, idProduct=pid, backend=backend())


def boot(firmware: Path = ROOT / "G25_MainFW_1.9.2.decrypted.img"):
    """Download to volatile RAM using the same endpoint as the original client."""
    if find_device() is not None:
        return "already running"
    device = find_device(0x03E7, 0x2150)
    if device is None:
        raise RuntimeError("未找到眼镜引导设备 03e7:2150 或 G25 设备 2e09:0030")
    payload = firmware.read_bytes()
    try:
        device.set_configuration()
        written = device.write(0x01, payload, timeout=15000)
        if written != len(payload):
            raise RuntimeError(f"固件传输不完整: {written}/{len(payload)}")
    finally:
        usb.util.dispose_resources(device)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if find_device() is not None:
            return f"RAM firmware loaded: {len(payload)} bytes"
        time.sleep(0.2)
    raise RuntimeError("固件已发送，等待 G25 枚举超时")


@dataclass
class LCDInfo:
    width: int
    height: int
    orientation: int
    rotation: int
    brightness: int


def decode_sensor(data: bytes, acc_fsr=1, gyro_fsr=3):
    if len(data) != 28:
        raise ValueError(f"Expected a 28-byte G25 sensor report, got {len(data)}")
    values = struct.unpack("<BBH9hBBf", data)
    acc_scale = (2, 4, 8, 16)[acc_fsr] / 32768
    gyro_scale = (250, 500, 1000, 2000)[gyro_fsr] / 32768
    acc = [v * acc_scale for v in values[6:9]]
    gyro = [v * gyro_scale for v in values[9:12]]
    x, y, z = acc
    return {
        "key_status": values[0],
        "key_code": values[1],
        "lux": values[2],
        "compass_raw": list(values[3:6]),
        "acc_raw": list(values[6:9]),
        "gyro_raw": list(values[9:12]),
        "acc_g": acc,
        "gyro_dps": gyro,
        "af_result": values[12],
        "caf_flag": values[13],
        "temperature_c": values[14] if math.isfinite(values[14]) else None,
        # Wearing-direction convention, confirmed on hardware: looking up is +.
        # Keep raw acceleration unchanged for tilt-compensated magnetometry.
        "pitch_deg": math.degrees(math.atan2(x, math.hypot(y, z))),
        "roll_deg": math.degrees(math.atan2(z, y)),
        "raw": data.hex(),
        "timestamp": time.time(),
    }


def yv12_image(data: bytes, width: int, height: int, target=None):
    """Decode planar Y, V, U with BT.601 limited-range conversion in Pillow."""
    size = width * height
    if len(data) != size * 3 // 2:
        raise ValueError("Incomplete YV12 frame")
    y = Image.frombytes("L", (width, height), data[:size])
    v = Image.frombytes("L", (width // 2, height // 2), data[size : size * 5 // 4])
    u = Image.frombytes("L", (width // 2, height // 2), data[size * 5 // 4 :])
    target = target or (width, height)
    y = y.point(LUMA_LUT)
    if y.size != target:
        y = y.resize(target, Image.Resampling.BILINEAR)
    u = u.point(CHROMA_LUT).resize(target, Image.Resampling.BILINEAR)
    v = v.point(CHROMA_LUT).resize(target, Image.Resampling.BILINEAR)
    return Image.merge("YCbCr", (y, u, v)).convert("RGB")


@lru_cache(maxsize=16)
def text_font(font_size):
    candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    # Font paths vary between distributions; fontconfig supplies the installed
    # CJK font when available, without requiring a particular package layout.
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "sans:lang=zh"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            candidates.insert(0, Path(result.stdout.strip()))
    except (OSError, subprocess.TimeoutExpired):
        pass
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), font_size)
            except OSError:
                pass
    return ImageFont.load_default(size=font_size)


def text_image(text, width=640, height=400, font_size=32):
    font = text_font(font_size)
    image = Image.new("RGB", (width, height), "black")
    draw = ImageDraw.Draw(image)
    draw.text((28, 18), "GLXSS / LIVE", font=font, fill="#53e5be")
    draw.line((28, 65, width - 28, 65), fill="#21443b", width=2)
    line, y = "", 90
    for character in text + "\n":
        if (
            character == "\n"
            or draw.textlength(line + character, font=font) > width - 56
        ):
            draw.text((28, y), line, font=font, fill="#eaf9f2")
            line, y = "", y + font_size + 12
            if y > height - font_size - 12:
                break
        if character != "\n":
            line += character
    return image


class Glasses:
    def __init__(self):
        self.dev = find_device()
        if self.dev is None:
            raise RuntimeError("未找到 G25 眼镜；请加载固件并检查 USB / WSL 连接")
        self.claimed = set()
        self.detached = set()
        self.camera_running = False
        self.camera_pump = None
        self.acc_fsr, self.gyro_fsr = 1, 3
        self.frame_buffer = bytearray()
        self.discarded_frames = 0
        self.sensor_original = None
        self.sensor_ack = {}

    def claim(self, interface):
        if interface in self.claimed:
            return
        try:
            if self.dev.is_kernel_driver_active(interface):
                self.dev.detach_kernel_driver(interface)
                self.detached.add(interface)
        except NotImplementedError:
            pass
        usb.util.claim_interface(self.dev, interface)
        self.claimed.add(interface)

    def control(self, request, value=0, interface=2, length=1):
        self.claim(interface)
        data = bytes(
            self.dev.ctrl_transfer(
                0xA1, request, value, interface, length, timeout=1500
            )
        )
        if len(data) != length:
            raise RuntimeError(
                f"Short control reply: request {request:#x}, {len(data)}/{length}"
            )
        return data

    def lcd_info(self):
        return LCDInfo(*struct.unpack("<HHBBH", self.control(4, interface=3, length=8)))

    def sensor_attributes(self):
        light = self.control(4, length=12)
        compass = self.control(5, length=12)
        imu = self.control(6, length=16)
        self.acc_fsr, self.gyro_fsr = imu[2], imu[5]
        if self.acc_fsr not in range(4) or self.gyro_fsr not in range(4):
            raise RuntimeError(f"Unsupported IMU range: {imu.hex()}")
        return {
            "light": {
                "status": light[0],
                "enabled": bool(light[1]),
                "chip_id": int.from_bytes(light[4:12], "little"),
            },
            "compass": {
                "status": compass[0],
                "enabled": bool(compass[1]),
                "chip_id": int.from_bytes(compass[4:12], "little"),
            },
            "imu": {
                "acc_status": imu[0],
                "acc_enabled": bool(imu[1]),
                "acc_fsr": imu[2],
                "gyro_status": imu[3],
                "gyro_enabled": bool(imu[4]),
                "gyro_fsr": imu[5],
                "chip_id": int.from_bytes(imu[8:16], "little"),
            },
        }

    def info(self):
        product = self.control(0x17, length=24)
        return {
            "vid": VID,
            "pid": PID,
            "firmware": product[:16].decode("ascii", errors="replace").rstrip("\x00"),
            "product_raw": product.hex(),
            "lcd": asdict(self.lcd_info()),
            "sensors": self.sensor_attributes(),
            "sensor_ack": self.sensor_ack,
            "usb_speed": {
                1: "USB 1.1 Low Speed · 1.5 Mb/s",
                2: "USB 1.1 Full Speed · 12 Mb/s",
                3: "USB 2.0 High Speed · 480 Mb/s",
                4: "USB 3.0 SuperSpeed · 5000 Mb/s",
                5: "USB SuperSpeed Plus",
            }.get(self.dev.speed, f"USB speed {self.dev.speed}"),
            "interfaces": 7,
        }

    def enable_sensors(self):
        attrs = self.sensor_attributes()
        if self.sensor_original is None:
            self.sensor_original = {
                0x10: attrs["light"]["enabled"],
                0x11: attrs["compass"]["enabled"],
                0x12: attrs["imu"]["acc_enabled"],
                0x14: attrs["imu"]["gyro_enabled"],
            }
        for name, request in (
            ("light", 0x10),
            ("compass", 0x11),
            ("accelerometer", 0x12),
            ("gyroscope", 0x14),
        ):
            self.sensor_ack[name] = self.control(request, 1)[0]
        self.sensor_attributes()
        return self.sensor_ack

    def sensor(self):
        return decode_sensor(self.control(0x16, length=28), self.acc_fsr, self.gyro_fsr)

    def lcd_brightness(self, value):
        if not 0 <= value <= 254:
            raise ValueError("Brightness must be 0..254")
        return self.control(1, value, interface=3)[0]

    def lcd_power(self, enabled):
        return self.control(2, int(enabled), interface=3)[0]

    def display(self, image):
        info = self.lcd_info()
        image = ImageOps.contain(image.convert("RGBA"), (info.width, info.height))
        canvas = Image.new("RGBA", (info.width, info.height), (0, 0, 0, 255))
        canvas.paste(
            image, ((info.width - image.width) // 2, (info.height - image.height) // 2)
        )
        # The real header is 16 bytes; the old client's comment said 20.
        packet = struct.pack("<IIB7x", info.width, info.height, 1) + canvas.tobytes()
        self.claim(3)
        count = self.dev.write(0x01, packet, timeout=3000)
        if count != len(packet):
            raise RuntimeError(f"Short LCD transfer: {count}/{len(packet)}")
        return count

    def camera_control(self, selector, data=None, length=1):
        self.claim(0)
        if data is None:
            result = bytes(
                self.dev.ctrl_transfer(
                    0xA1, 0x81, selector, 0x0400, length, timeout=3000
                )
            )
            if len(result) != length:
                raise RuntimeError("Short camera extension reply")
            return result
        result = self.dev.ctrl_transfer(0x21, 1, selector, 0x0400, data, timeout=3000)
        if result != len(data):
            raise RuntimeError("Short camera extension write")
        return result

    def frame_intervals(self, frame_index):
        descriptors = bytes(
            self.dev.get_active_configuration()[(1, 0)].extra_descriptors
        )
        offset = 0
        while offset + 3 <= len(descriptors):
            length = descriptors[offset]
            if length < 3 or offset + length > len(descriptors):
                break
            item = descriptors[offset : offset + length]
            if item[1:4] == bytes((0x24, 5, frame_index)) and length >= 26:
                count = item[25]
                if count and length >= 26 + count * 4:
                    return sorted(set(struct.unpack_from(f"<{count}I", item, 26)))
            offset += length
        return [333333]

    def camera_start(self, mode="640x480", fps=None):
        if self.camera_running:
            self.camera_stop()
        frame_index, self.width, self.height = MODES[mode]
        intervals = self.frame_intervals(frame_index)
        interval = (
            intervals[0]
            if not fps
            else min(intervals, key=lambda value: abs(10_000_000 / value - fps))
        )
        self.claim(0)
        self.claim(1)
        self.camera_control(0x0D0D, b"\x01")
        self.camera_running = True
        try:
            time.sleep(1)  # Official SDK waits for sensor/ISP startup.
            probe = bytearray(34)
            struct.pack_into("<HBBI", probe, 0, 1, 1, frame_index, interval)
            struct.pack_into("<II", probe, 18, self.width * self.height * 3 // 2, 16384)
            self.dev.ctrl_transfer(0x21, 1, 0x0100, 1, probe, timeout=3000)
            agreed = bytes(
                self.dev.ctrl_transfer(0xA1, 0x81, 0x0100, 1, 34, timeout=3000)
            )
            if len(agreed) != 34 or agreed[2:4] != bytes((1, frame_index)):
                raise RuntimeError(f"Unexpected UVC probe: {agreed.hex()}")
            self.dev.ctrl_transfer(0x21, 1, 0x0200, 1, agreed, timeout=3000)
            # Keep multiple IN transfers queued so SuperSpeed DMA never waits on
            # JPEG conversion, sensor controls or GUI rendering.
            usb.util.release_interface(self.dev, 1)
            self.claimed.discard(1)
            from usb_stream import CameraPump

            self.camera_pump = CameraPump(self.width, self.height)
            self.frame_buffer.clear()
            self.discarded_frames = 0
            agreed_interval = struct.unpack_from("<I", agreed, 4)[0]
            return {
                "mode": mode,
                "probe": agreed.hex(),
                "pixel_format": "YV12",
                "requested_fps": round(10_000_000 / interval, 2),
                "agreed_fps": round(10_000_000 / agreed_interval, 2)
                if agreed_interval
                else None,
                "software_limit": None,
            }
        except Exception:
            self.camera_stop()
            raise

    def camera_read(self, decode=True):
        """G25 sends one UVC payload per frame, larger than negotiated max payload.

        Only the first bulk chunk has a header. EOF is already set there. A USB
        short packet ends the payload; removing a header from every chunk corrupts it.
        The first frame can still be the ISP's previous resolution, so reject sizes.
        """
        if not self.camera_running:
            raise RuntimeError("Camera is off")
        if self.camera_pump is not None:
            frame = self.camera_pump.read()
            if frame is None:
                return None
            return yv12_image(frame, self.width, self.height) if decode else frame
        chunk = bytes(self.dev.read(0x81, 65536, timeout=700))
        self.frame_buffer.extend(chunk)
        if len(self.frame_buffer) > 32 * 1024 * 1024:
            self.frame_buffer.clear()
            raise RuntimeError("Camera payload exceeded 32 MiB")
        if len(chunk) == 65536:
            return None
        packet = bytes(self.frame_buffer)
        self.frame_buffer.clear()
        expected = self.width * self.height * 3 // 2
        if (
            len(packet) < 2
            or packet[0] < 2
            or packet[0] > len(packet)
            or packet[1] & 0x40
        ):
            self.discarded_frames += 1
            return None
        frame = packet[packet[0] :]
        if len(frame) != expected:
            self.discarded_frames += 1
            return None
        return yv12_image(frame, self.width, self.height)

    def camera_stop(self):
        if self.camera_running:
            try:
                self.camera_control(0x0D0D, b"\x00")
            finally:
                self.camera_running = False
                self.frame_buffer.clear()
                if self.camera_pump:
                    self.camera_pump.close()
                    self.camera_pump = None

    def close(self):
        try:
            self.camera_stop()
            if self.sensor_original:
                for request, enabled in self.sensor_original.items():
                    self.control(request, int(enabled))
        finally:
            usb.util.dispose_resources(self.dev)
            for interface in self.detached:
                try:
                    self.dev.attach_kernel_driver(interface)
                except usb.core.USBError:
                    pass
            self.claimed.clear()
            self.detached.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("boot")
    command.add_argument(
        "--firmware", type=Path, default=ROOT / "G25_MainFW_1.9.2.decrypted.img"
    )
    sub.add_parser("info")
    command = sub.add_parser("sensors")
    command.add_argument("--seconds", type=float, default=10)
    command = sub.add_parser("capture")
    command.add_argument("output", type=Path)
    command.add_argument("--mode", choices=MODES, default="640x480")
    command = sub.add_parser("display")
    command.add_argument("--text")
    command.add_argument("--image", type=Path)
    args = parser.parse_args()
    if args.command == "boot":
        print(boot(args.firmware))
        return
    with Glasses() as glasses:
        if args.command == "info":
            print(json.dumps(glasses.info(), indent=2, ensure_ascii=False))
        elif args.command == "sensors":
            print(json.dumps({"ack": glasses.enable_sensors()}))
            until = time.monotonic() + args.seconds
            while time.monotonic() < until:
                print(json.dumps(glasses.sensor(), ensure_ascii=False), flush=True)
                time.sleep(0.1)
        elif args.command == "capture":
            glasses.camera_start(args.mode)
            until = time.monotonic() + 15
            while time.monotonic() < until:
                image = glasses.camera_read()
                if image is not None:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    image.save(args.output)
                    print(f"Saved {args.output} ({image.width}x{image.height})")
                    break
            else:
                raise RuntimeError(
                    "No complete camera frame received within 15 seconds"
                )
        elif args.command == "display":
            if args.text is None and args.image is None:
                parser.error("display requires --text or --image")
            image = (
                text_image(args.text)
                if args.text is not None
                else Image.open(args.image)
            )
            print(f"Sent {glasses.display(image)} bytes")


if __name__ == "__main__":
    main()
