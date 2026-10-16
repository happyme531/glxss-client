"""Private local stdio bridge: PyQt frontend <-> USB worker.

No network listener. Separate workers own camera and LCD USB interfaces. JSON
lines on stdout, commands on stdin. Closing stdin restores sensor switches.
"""

import argparse
import base64
import io
import json
import queue
import sys
import threading
import time
from contextlib import ExitStack

import usb.core
import usb.util
from PIL import Image

from glxss import Glasses, text_image
from interaction import ButtonTracker
from lcd_stream import LCDPump
from video import FrameConverter

OUTPUT_LOCK = threading.Lock()


def emit(kind, **data):
    with OUTPUT_LOCK:
        print(
            json.dumps({"type": kind, **data}, ensure_ascii=False, allow_nan=False),
            flush=True,
        )


def commands(target):
    try:
        for line in sys.stdin:
            try:
                target.put(json.loads(line))
            except (ValueError, TypeError) as error:
                target.put({"op": "invalid", "error": str(error)})
    finally:
        target.put({"op": "quit"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-buffer")
    args = parser.parse_args()
    pending = queue.Queue()
    threading.Thread(target=commands, args=(pending,), daemon=True).start()
    with ExitStack() as stack:
        preview = None
        if args.preview_buffer:
            from frame_transport import PreviewBuffer

            preview = stack.enter_context(PreviewBuffer(args.preview_buffer))
        preview_converter = stack.enter_context(FrameConverter())
        screen_converter = stack.enter_context(FrameConverter())
        photo_converter = stack.enter_context(FrameConverter())
        glasses = stack.enter_context(Glasses())
        glasses.enable_sensors()
        info = glasses.info()
        usb.util.release_interface(glasses.dev, 3)
        glasses.claimed.discard(3)
        screen = stack.enter_context(LCDPump(emit))
        emit("connected", info=info)
        emit("log", message="USB 已连接；光照、加速度计、陀螺仪已开启。")
        emit(
            "log",
            message=f"预览：{'共享内存' if preview else 'JPEG'} · {preview_converter.backend} · 原始分辨率拍照",
        )
        emit("camera", running=False)
        poll_at = time.monotonic()
        mirror = False
        keys = ButtonTracker()
        preview_active = False
        stream = {}
        audio_held = False
        resume_stream = None
        resume_preview = False
        last_raw = None
        preview_limit = (1280, 800)
        stats_at = time.monotonic()
        previous_stats = {"received": 0, "bytes": 0, "elapsed": 0}
        while True:
            try:
                command = pending.get_nowait()
            except queue.Empty:
                command = None
            if command is not None:
                op = command.get("op")
                try:
                    if op == "quit":
                        break
                    elif op == "camera_start":
                        if audio_held:
                            raise RuntimeError("音频播放期间摄像头已暂停")
                        mode = command.get("mode", "640x480")
                        requested_fps = command.get("fps")
                        if (
                            not glasses.camera_running
                            or mode != stream["mode"]
                            or requested_fps != stream.get("fps_option")
                        ):
                            stream = glasses.camera_start(mode, requested_fps)
                            stream["fps_option"] = requested_fps
                            previous_stats = {"received": 0, "bytes": 0, "elapsed": 0}
                            last_raw = None
                        preview_active = True
                        emit("camera", running=True, stream=stream)
                    elif op == "preview_pause":
                        preview_active = False
                        emit("camera", running=False, capturing=glasses.camera_running)
                    elif op == "camera_stop":
                        resume_stream = None
                        glasses.camera_stop()
                        emit("camera", running=False)
                    elif op == "preview_size":
                        preview_limit = (
                            max(240, min(1280, int(command["width"]))),
                            max(180, min(800, int(command["height"]))),
                        )
                    elif op == "save_photo":
                        if last_raw is None:
                            raise RuntimeError("尚无完整摄像头画面")
                        from pathlib import Path

                        path = Path(command["path"])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        photo_converter.image(
                            last_raw, glasses.width, glasses.height
                        ).save(path)
                        emit(
                            "photo",
                            path=str(path),
                            width=glasses.width,
                            height=glasses.height,
                        )
                    elif op == "audio_hold":
                        enabled = bool(command["enabled"])
                        if enabled and not audio_held:
                            resume_stream = (
                                dict(stream) if glasses.camera_running else None
                            )
                            resume_preview = preview_active
                            screen.hold_for_audio(True)
                            glasses.camera_stop()
                            audio_held = True
                            emit(
                                "camera",
                                running=False,
                                capturing=False,
                                audio_suspended=True,
                            )
                        elif not enabled and audio_held:
                            screen.hold_for_audio(False)
                            audio_held = False
                            if resume_stream is not None:
                                mode, fps = (
                                    resume_stream["mode"],
                                    resume_stream.get("fps_option"),
                                )
                                stream = glasses.camera_start(mode, fps)
                                stream["fps_option"] = fps
                                preview_active = resume_preview
                                previous_stats = {
                                    "received": 0,
                                    "bytes": 0,
                                    "elapsed": 0,
                                }
                            resume_stream = None
                            emit(
                                "camera",
                                running=glasses.camera_running and preview_active,
                                capturing=glasses.camera_running,
                                audio_suspended=False,
                                stream=stream,
                            )
                        emit("response", op=op, ok=True, enabled=audio_held)
                        continue
                    elif op == "display_text":
                        image = text_image(str(command.get("text", ""))[:1000])
                        mirror = False
                        screen.command("image", image=image, message="文字已投送到眼镜")
                    elif op == "display_image":
                        raw = base64.b64decode(command["data"], validate=True)
                        if len(raw) > 16 * 1024 * 1024:
                            raise ValueError("Image exceeds 16 MiB")
                        image = Image.open(io.BytesIO(raw))
                        image.load()
                        mirror = False
                        screen.command("image", image=image, message="图片已投送到眼镜")
                    elif op == "brightness":
                        value = int(command["value"])
                        screen.command("brightness", value=value)
                    elif op == "lcd_power":
                        screen.command("power", enabled=bool(command["enabled"]))
                    elif op == "mirror":
                        mirror = bool(command["enabled"])
                        screen.command("mirror", enabled=mirror)
                    elif op == "refresh":
                        info["sensors"] = glasses.sensor_attributes()
                        emit("connected", info=info)
                    else:
                        raise ValueError(f"Unknown command: {op}")
                    emit("response", op=op, ok=True)
                except Exception as error:
                    emit("error", op=op, message=str(error))
            now = time.monotonic()
            if now >= poll_at:
                try:
                    sensor = glasses.sensor()
                    emit("sensor", data=sensor)
                    key = keys.update(sensor["key_code"], sensor["key_status"], now)
                    if key:
                        emit("key", **key, timestamp=sensor["timestamp"])
                except usb.core.USBError as error:
                    emit("error", op="sensor", message=str(error))
                    if error.errno == 19:
                        break
                poll_at = now + 0.02
            if glasses.camera_running:
                try:
                    raw = glasses.camera_read(decode=False)
                    if raw is not None:
                        last_raw = raw
                        if mirror:
                            screen.submit(
                                screen_converter.image(
                                    raw, glasses.width, glasses.height, (640, 400)
                                )
                            )
                        if preview_active:
                            stamp = time.time()
                            if preview:
                                pixels, width, height = preview_converter.convert(
                                    raw, glasses.width, glasses.height, preview_limit
                                )
                                sequence = preview.write(
                                    pixels,
                                    width,
                                    height,
                                    glasses.width,
                                    glasses.height,
                                    stamp,
                                )
                                emit(
                                    "frame",
                                    sequence=sequence,
                                    width=glasses.width,
                                    height=glasses.height,
                                    timestamp=stamp,
                                )
                            else:
                                image = preview_converter.image(
                                    raw, glasses.width, glasses.height, preview_limit
                                )
                                emit(
                                    "frame",
                                    jpeg=encode_image(image),
                                    width=glasses.width,
                                    height=glasses.height,
                                    timestamp=stamp,
                                )
                except usb.core.USBTimeoutError:
                    emit("log", message="摄像头等待数据超时")
                    time.sleep(0.1)
                except Exception as error:
                    emit("error", op="camera", message=str(error))
                    glasses.camera_stop()
                    emit("camera", running=False)
            else:
                time.sleep(0.01)
            if glasses.camera_pump and now >= stats_at:
                stats = glasses.camera_pump.stats()
                elapsed = max(0.001, stats["elapsed"] - previous_stats["elapsed"])
                emit(
                    "camera_stats",
                    **stats,
                    source_fps=(stats["received"] - previous_stats["received"])
                    / elapsed,
                    mbps=(stats["bytes"] - previous_stats["bytes"])
                    / elapsed
                    / 1_000_000,
                )
                previous_stats = stats
                stats_at = now + 1


def encode_image(image):
    output = io.BytesIO()
    image = image.convert("RGB")
    image.save(output, "JPEG", quality=82)
    return base64.b64encode(output.getvalue()).decode("ascii")


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception as error:
        emit("fatal", message=str(error))
        sys.exit(1)
