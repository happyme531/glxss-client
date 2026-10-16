"""Latest-frame LCD sender with its own USB interface and no software frame cap."""

import base64
import io
import queue
import struct
import threading
import time
from collections import deque

import usb1
from PIL import Image, ImageOps


def lcd_packet(image, width, height):
    fitted = ImageOps.contain(
        image.convert("RGB"), (width, height), Image.Resampling.BILINEAR
    )
    canvas = Image.new("RGB", (width, height), "black")
    canvas.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    return struct.pack("<IIB7x", width, height, 1) + canvas.convert(
        "RGBA"
    ).tobytes(), canvas


class LCDPump:
    def __init__(self, emit):
        self.emit = emit
        self.commands = queue.Queue()
        self.frames = queue.Queue(maxsize=1)
        self.ready = threading.Event()
        self.stopping = threading.Event()
        self.mirror = False
        self.audio_held = False
        self.error = None
        self.sent = self.superseded = 0
        self.transfer_times = deque(maxlen=60)
        self.age_ms = 0.0
        self.thread = threading.Thread(
            target=self.run, name="glxss-lcd-usb", daemon=True
        )
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            self.close()
            raise RuntimeError(self.error or "LCD worker startup timed out")

    def command(self, op, **data):
        self.commands.put({"op": op, **data})

    def hold_for_audio(self, enabled):
        done = threading.Event()
        self.command("audio_hold", enabled=enabled, done=done)
        if not done.wait(3) or self.error:
            raise RuntimeError(self.error or "等待显示屏停止视频传输超时")

    def submit(self, image):
        if self.frames.full():
            try:
                self.frames.get_nowait()
                self.superseded += 1
            except queue.Empty:
                pass
        self.frames.put_nowait((image, time.monotonic()))

    def run(self):
        with usb1.USBContext() as context:
            handle = None
            try:
                handle = context.openByVendorIDAndProductID(0x2E09, 0x30)
                if handle is None:
                    raise RuntimeError("LCD USB device disappeared")
                handle.claimInterface(3)
                info = struct.unpack(
                    "<HHBBH", handle.controlRead(0xA1, 4, 0, 3, 8, timeout=1500)
                )
                width, height = info[:2]
                self.ready.set()
                stats_at = time.monotonic()
                previous_time, previous_sent = stats_at, 0
                while not self.stopping.is_set():
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        command = None
                    if command:
                        op = command["op"]
                        try:
                            if op == "mirror":
                                self.mirror = bool(command["enabled"])
                                if not self.mirror:
                                    while not self.frames.empty():
                                        self.frames.get_nowait()
                                self.emit("mirror", enabled=self.mirror)
                            elif op == "audio_hold":
                                self.audio_held = bool(command["enabled"])
                                while not self.frames.empty():
                                    self.frames.get_nowait()
                                command["done"].set()
                            elif op == "image":
                                self.mirror = False
                                self.emit("mirror", enabled=False)
                                packet, canvas = lcd_packet(
                                    command["image"], width, height
                                )
                                if handle.bulkWrite(1, packet, timeout=2000) != len(
                                    packet
                                ):
                                    raise RuntimeError("Short LCD image write")
                                output = io.BytesIO()
                                canvas.save(output, "JPEG", quality=82)
                                self.emit(
                                    "display",
                                    message=command["message"],
                                    preview=base64.b64encode(
                                        output.getvalue()
                                    ).decode(),
                                )
                            elif op == "brightness":
                                value = int(command["value"])
                                if not 0 <= value <= 254:
                                    raise ValueError("Brightness must be 0..254")
                                handle.controlRead(0xA1, 1, value, 3, 1, timeout=1500)
                                values = struct.unpack(
                                    "<HHBBH",
                                    handle.controlRead(0xA1, 4, 0, 3, 8, timeout=1500),
                                )
                                self.emit(
                                    "lcd",
                                    info=dict(
                                        zip(
                                            (
                                                "width",
                                                "height",
                                                "orientation",
                                                "rotation",
                                                "brightness",
                                            ),
                                            values,
                                        )
                                    ),
                                )
                            elif op == "power":
                                handle.controlRead(
                                    0xA1, 2, int(command["enabled"]), 3, 1, timeout=1500
                                )
                                self.emit(
                                    "log",
                                    message="眼镜显示屏已"
                                    + ("开启" if command["enabled"] else "关闭"),
                                )
                        except Exception as error:
                            self.emit("error", op="lcd_" + op, message=str(error))
                    if self.mirror and not self.audio_held:
                        try:
                            image, queued_at = self.frames.get(timeout=0.005)
                        except queue.Empty:
                            image = None
                        if image is not None:
                            packet, _ = lcd_packet(image, width, height)
                            start = time.monotonic()
                            written = handle.bulkWrite(1, packet, timeout=2000)
                            if written != len(packet):
                                raise RuntimeError(
                                    f"Short LCD write {written}/{len(packet)}"
                                )
                            self.transfer_times.append(time.monotonic() - start)
                            self.age_ms = (time.monotonic() - queued_at) * 1000
                            self.sent += 1
                    else:
                        self.stopping.wait(0.005)
                    now = time.monotonic()
                    if now >= stats_at:
                        elapsed = max(0.001, now - previous_time)
                        fps = (self.sent - previous_sent) / elapsed
                        self.emit(
                            "mirror_stats",
                            enabled=self.mirror,
                            audio_suspended=self.audio_held,
                            fps=fps,
                            sent=self.sent,
                            superseded=self.superseded,
                            age_ms=self.age_ms,
                            transfer_ms=(
                                sum(self.transfer_times)
                                / len(self.transfer_times)
                                * 1000
                                if self.transfer_times
                                else 0
                            ),
                            mbps=fps * (width * height * 4 + 16) / 1_000_000,
                        )
                        previous_time, previous_sent = now, self.sent
                        stats_at = now + 1
            except Exception as error:
                self.error = str(error)
                self.ready.set()
                self.emit("error", op="lcd", message=self.error)
            finally:
                if handle:
                    try:
                        handle.releaseInterface(3)
                    except usb1.USBError:
                        pass
                    finally:
                        handle.close()

    def close(self):
        self.stopping.set()
        self.thread.join(4)
        if self.thread.is_alive():
            raise RuntimeError("LCD worker did not stop")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
