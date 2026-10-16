"""Local JSON-lines worker for USB audio, sharing no USB interfaces with video."""

import json
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from audio import USBAudio, load_wav, tone_pcm

ROOT = Path(__file__).resolve().parent


def emit(kind, **data):
    print(
        json.dumps({"type": kind, **data}, ensure_ascii=False, allow_nan=False),
        flush=True,
    )


def main():
    pending = queue.Queue()

    def reader():
        try:
            for line in sys.stdin:
                pending.put(json.loads(line))
        finally:
            pending.put({"op": "quit"})

    threading.Thread(target=reader, daemon=True).start()
    with USBAudio(emit) as audio:
        emit("audio_connected", info=audio.info())
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
                    elif op == "capture_start":
                        audio.capture_start()
                        emit("microphone", active=True)
                    elif op == "capture_stop":
                        audio.capture_stop()
                        emit("microphone", active=False)
                    elif op == "record_start":
                        path = (
                            ROOT
                            / "captures"
                            / "audio"
                            / (
                                datetime.now().strftime("glxss-%Y%m%d-%H%M%S-%f")
                                + ".wav"
                            )
                        )
                        audio.record_start(path)
                        emit("microphone", active=True)
                    elif op == "record_stop":
                        audio.record_stop()
                    elif op == "tone":
                        audio.play(
                            tone_pcm(),
                            "低音量测试音 · 660 Hz",
                            command.get("request_id"),
                        )
                    elif op == "play":
                        path = Path(command["path"]).resolve()
                        audio.play(load_wav(path), path.name, command.get("request_id"))
                    elif op == "play_stop":
                        audio.play_stop()
                        emit("playback_stopped", request_id=command.get("request_id"))
                    elif op == "volume":
                        audio.volume(command["target"], command["percent"])
                        emit("audio_mixer", mixer=audio.mixer)
                    elif op == "mute":
                        audio.mute(command["target"], bool(command["enabled"]))
                        emit("audio_mixer", mixer=audio.mixer)
                    else:
                        raise ValueError(f"Unknown audio command: {op}")
                except Exception as error:
                    if op in ("tone", "play"):
                        audio.play_stop()
                    emit(
                        "error",
                        op=op,
                        message=str(error),
                        request_id=command.get("request_id"),
                    )
            audio.poll()
            if not audio.capture_active and not audio.play_active:
                time.sleep(0.02)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception as error:
        emit("fatal", message=str(error))
        sys.exit(1)
