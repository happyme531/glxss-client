"""Native USB Audio Class 1 transport; no ALSA kernel or desktop sound server needed."""

import math
import struct
import time
import wave
from array import array
from pathlib import Path

import usb1

RATE = 44100
CHANNELS = 2
FRAME_BYTES = 4


def pcm_levels(pcm):
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 4 * 4])
    channels = [samples[::2], samples[1::2]]
    rms, peaks = [], []
    for values in channels:
        power = sum(value * value for value in values) / max(1, len(values))
        rms.append(max(-90.0, 20 * math.log10(max(1e-9, math.sqrt(power) / 32768))))
        peaks.append(
            max(
                -90.0,
                20 * math.log10(max(1e-9, max(map(abs, values), default=0) / 32768)),
            )
        )
    return rms, peaks


def tone_pcm(seconds=0.7, frequency=660.0, level=0.035):
    output = array("h")
    count = round(seconds * RATE)
    for i in range(count):
        envelope = min(1.0, i / (RATE * 0.015), (count - 1 - i) / (RATE * 0.015))
        value = round(
            32767 * level * envelope * math.sin(2 * math.pi * frequency * i / RATE)
        )
        output.extend((value, value))
    return output.tobytes()


def load_wav(path):
    with wave.open(str(path), "rb") as source:
        if (
            source.getsampwidth() != 2
            or source.getframerate() != RATE
            or source.getnchannels() not in (1, 2)
        ):
            raise ValueError("请选择 44.1 kHz、16 位 PCM WAV（单声道或双声道）。")
        if source.getnframes() > RATE * 60 * 20:
            raise ValueError("单次播放文件请控制在 20 分钟以内。")
        pcm = source.readframes(source.getnframes())
        if source.getnchannels() == 1:
            samples, stereo = array("h"), array("h")
            samples.frombytes(pcm)
            for value in samples:
                stereo.extend((value, value))
            pcm = stereo.tobytes()
        return pcm


def quantized_volume(percent, minimum, maximum, resolution):
    if percent <= 0:
        return minimum
    if percent >= 100:
        return maximum
    requested = minimum + max(0.0, min(100.0, percent)) / 100 * (maximum - minimum)
    step = max(1, resolution)
    value = minimum + round((requested - minimum) / step) * step
    return max(minimum, min(maximum, value))


class USBAudio:
    def __init__(self, emit=lambda *_args, **_kwargs: None, queue_depth=32):
        self.emit = emit
        self.queue_depth = queue_depth
        self.context = usb1.USBContext()
        self.handle = self.context.openByVendorIDAndProductID(0x2E09, 0x30)
        if self.handle is None:
            self.context.close()
            raise RuntimeError("未找到眼镜 USB 音频接口")
        self.claimed = set()
        self.detached = set()
        self.capture_active = self.play_active = False
        self.capture_transfers, self.play_transfers = [], []
        self.recording = None
        self.record_path = None
        self.record_bytes = 0
        self.capture_bytes = self.capture_packets = self.packet_errors = 0
        self.meter_buffer = bytearray()
        self.meter_at = time.monotonic()
        self.play_pcm = b""
        self.play_pos = self.play_completed = self.play_fraction = 0
        self.play_pending_end = False
        self.play_title = ""
        self.play_request = None
        self.mixer = {}
        try:
            self.claim(4)
            self.mixer = {
                "speaker": self.read_mixer(3),
                "microphone": self.read_mixer(6),
            }
        except Exception:
            self.close()
            raise

    def claim(self, interface):
        if interface in self.claimed:
            return
        if self.handle.kernelDriverActive(interface):
            self.handle.detachKernelDriver(interface)
            self.detached.add(interface)
        self.handle.claimInterface(interface)
        self.claimed.add(interface)

    def read_mixer(self, unit):
        def read(request):
            return struct.unpack(
                "<h",
                self.handle.controlRead(
                    0xA1, request, 0x0201, (unit << 8) | 4, 2, timeout=1000
                ),
            )[0]

        minimum, maximum, resolution, value = (
            read(0x82),
            read(0x83),
            read(0x84),
            read(0x81),
        )
        mute = bool(
            self.handle.controlRead(
                0xA1, 0x81, 0x0100, (unit << 8) | 4, 1, timeout=1000
            )[0]
        )
        return {
            "unit": unit,
            "minimum": minimum,
            "maximum": maximum,
            "resolution": resolution,
            "value": value,
            "db": value / 256,
            "minimum_db": minimum / 256,
            "maximum_db": maximum / 256,
            "percent": round(100 * (value - minimum) / max(1, maximum - minimum)),
            "muted": mute,
        }

    def info(self):
        return {
            "sample_rate": RATE,
            "channels": CHANNELS,
            "sample_bits": 16,
            "transport": "USB isochronous",
            "mixer": self.mixer,
            "capture_active": self.capture_active,
            "playing": self.play_active,
        }

    def volume(self, target, percent):
        settings = self.mixer[target]
        value = quantized_volume(
            float(percent),
            settings["minimum"],
            settings["maximum"],
            settings["resolution"],
        )
        for channel in (1, 2):
            self.handle.controlWrite(
                0x21,
                1,
                0x0200 | channel,
                (settings["unit"] << 8) | 4,
                struct.pack("<h", value),
                timeout=1000,
            )
        self.mixer[target] = self.read_mixer(settings["unit"])
        return self.mixer[target]

    def mute(self, target, enabled):
        settings = self.mixer[target]
        self.handle.controlWrite(
            0x21,
            1,
            0x0100,
            (settings["unit"] << 8) | 4,
            bytes((int(enabled),)),
            timeout=1000,
        )
        self.mixer[target] = self.read_mixer(settings["unit"])
        return self.mixer[target]

    def capture_start(self):
        if self.capture_active:
            return
        self.claim(6)
        self.handle.setInterfaceAltSetting(6, 1)
        self.capture_active = True
        self.capture_bytes = self.capture_packets = self.packet_errors = 0
        self.meter_buffer.clear()
        self.capture_transfers = []
        # One packet per URB works across native Linux and USB/IP transports.
        # Batch URBs can expose unused microframe slots as per-packet failures.
        for _ in range(self.queue_depth):
            transfer = self.handle.getTransfer(iso_packets=1)
            transfer.setIsochronous(
                0x82,
                220,
                callback=self.capture_callback,
                timeout=1000,
                iso_transfer_length_list=[220],
            )
            self.capture_transfers.append(transfer)
            transfer.submit()

    def capture_callback(self, transfer):
        status = transfer.getStatus()
        if status == usb1.TRANSFER_COMPLETED:
            for packet_status, buffer in transfer.iterISO():
                if packet_status == usb1.TRANSFER_COMPLETED:
                    pcm = bytes(buffer)
                    pcm = pcm[: len(pcm) // FRAME_BYTES * FRAME_BYTES]
                    self.capture_packets += 1
                    self.capture_bytes += len(pcm)
                    self.meter_buffer.extend(pcm)
                    if self.recording:
                        self.recording.writeframesraw(pcm)
                        self.record_bytes += len(pcm)
                else:
                    self.packet_errors += 1
        elif status not in (usb1.TRANSFER_CANCELLED, usb1.TRANSFER_TIMED_OUT):
            self.emit("error", op="microphone", message=f"USB 音频读取状态 {status}")
            self.capture_active = False
        if self.capture_active:
            transfer.submit()

    def record_start(self, path):
        if self.recording:
            raise RuntimeError("已经在录音")
        self.capture_start()
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        recording = wave.open(str(path), "wb")
        recording.setnchannels(CHANNELS)
        recording.setsampwidth(2)
        recording.setframerate(RATE)
        self.recording, self.record_path, self.record_bytes = recording, path, 0
        self.emit("recording", active=True, path=str(path))

    def record_stop(self):
        if self.recording:
            self.recording.close()
            self.recording = None
            self.emit(
                "recording",
                active=False,
                path=str(self.record_path),
                seconds=self.record_bytes / (RATE * FRAME_BYTES),
                bytes=self.record_bytes,
            )

    def play(self, pcm, title="音频", request_id=None):
        self.play_stop()
        if not pcm or len(pcm) % FRAME_BYTES:
            raise ValueError("Invalid stereo PCM data")
        self.claim(5)
        self.handle.setInterfaceAltSetting(5, 1)
        self.play_pcm, self.play_title = pcm, title
        self.play_request = request_id
        self.play_pos = self.play_completed = self.play_fraction = 0
        self.play_pending_end = False
        self.play_active = True
        self.play_transfers = []
        for _ in range(self.queue_depth):
            transfer = self.handle.getTransfer(iso_packets=1)
            if not self.fill_playback(transfer):
                break
            self.play_transfers.append(transfer)
            transfer.submit()
        self.emit(
            "playback",
            active=True,
            title=title,
            seconds=len(pcm) / (RATE * FRAME_BYTES),
            request_id=self.play_request,
        )

    def fill_playback(self, transfer):
        if self.play_pos >= len(self.play_pcm):
            return False
        self.play_fraction += RATE
        frames, self.play_fraction = divmod(self.play_fraction, 1000)
        length = frames * FRAME_BYTES
        payload = self.play_pcm[self.play_pos : self.play_pos + length]
        self.play_pos += len(payload)
        transfer.setIsochronous(
            0x02,
            payload,
            callback=self.play_callback,
            timeout=1000,
            iso_transfer_length_list=[len(payload)],
        )
        return True

    def play_callback(self, transfer):
        status = transfer.getStatus()
        if status == usb1.TRANSFER_COMPLETED:
            for packet_status, buffer in transfer.iterISO():
                if packet_status != 0:
                    self.packet_errors += 1
            self.play_completed += 1
        elif status not in (usb1.TRANSFER_CANCELLED, usb1.TRANSFER_TIMED_OUT):
            self.emit(
                "error",
                op="speaker",
                message=f"USB 音频播放状态 {status}",
                request_id=self.play_request,
            )
            self.play_pending_end = True
            self.play_pos = len(self.play_pcm)
        if self.play_active and self.fill_playback(transfer):
            transfer.submit()
        elif self.play_active:
            self.play_pending_end = True

    def cancel(self, transfers):
        for transfer in transfers:
            if transfer.isSubmitted():
                try:
                    transfer.cancel()
                except usb1.USBErrorNotFound:
                    pass
        deadline = time.monotonic() + 2
        while any(t.isSubmitted() for t in transfers) and time.monotonic() < deadline:
            self.context.handleEventsTimeout(0.01)

    def capture_stop(self):
        self.record_stop()
        self.capture_active = False
        self.cancel(self.capture_transfers)
        self.capture_transfers.clear()
        if 6 in self.claimed:
            self.handle.setInterfaceAltSetting(6, 0)

    def play_stop(self):
        was_playing = self.play_active
        self.play_active = False
        self.play_pending_end = False
        self.cancel(self.play_transfers)
        self.play_transfers.clear()
        if 5 in self.claimed:
            self.handle.setInterfaceAltSetting(5, 0)
        if was_playing:
            self.emit(
                "playback",
                active=False,
                title=self.play_title,
                request_id=self.play_request,
            )

    def poll(self):
        self.context.handleEventsTimeout(0.01)
        if self.play_pending_end and not any(
            t.isSubmitted() for t in self.play_transfers
        ):
            self.play_stop()
        now = time.monotonic()
        if self.capture_active and now - self.meter_at >= 0.08:
            if self.meter_buffer:
                rms, peaks = pcm_levels(self.meter_buffer)
                self.emit(
                    "audio_level",
                    rms_db=rms,
                    peak_db=peaks,
                    bytes=self.capture_bytes,
                    errors=self.packet_errors,
                    record_seconds=self.record_bytes / (RATE * FRAME_BYTES),
                )
                self.meter_buffer.clear()
            self.meter_at = now

    def close(self):
        try:
            self.capture_stop()
            self.play_stop()
        finally:
            if self.handle:
                for interface in sorted(self.claimed, reverse=True):
                    try:
                        self.handle.releaseInterface(interface)
                        if interface in self.detached:
                            self.handle.attachKernelDriver(interface)
                    except usb1.USBError:
                        pass
                self.handle.close()
                self.handle = None
            self.context.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
