"""Continuously queued USB camera reads, independent of image conversion/UI speed."""

import queue
import threading
import time

import usb1


class CameraPump:
    def __init__(self, width, height, chunk_size=262144, queue_depth=8):
        self.expected = width * height * 3 // 2
        self.chunk_size, self.queue_depth = chunk_size, queue_depth
        self.frames = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.received = self.skipped = self.invalid = self.bytes_received = 0
        self.started = time.monotonic()
        self.thread = threading.Thread(
            target=self.run, name="glxss-camera-usb", daemon=True
        )
        self.thread.start()
        if not self.ready.wait(5):
            self.close()
            raise RuntimeError("Camera USB worker startup timed out")
        if self.error:
            self.close()
            raise RuntimeError(self.error)

    def run(self):
        transfers, packet = [], bytearray()
        with usb1.USBContext() as context:
            handle = None
            try:
                handle = context.openByVendorIDAndProductID(0x2E09, 0x30)
                if handle is None:
                    raise RuntimeError("Camera USB device disappeared")
                handle.claimInterface(1)
                handle.clearHalt(0x81)

                def callback(transfer):
                    nonlocal packet
                    status = transfer.getStatus()
                    if status == usb1.TRANSFER_COMPLETED:
                        length = transfer.getActualLength()
                        packet.extend(transfer.getBuffer()[:length])
                        self.bytes_received += length
                        if length < self.chunk_size:
                            if (
                                len(packet) > 1
                                and 2 <= packet[0] <= len(packet)
                                and not packet[1] & 0x40
                            ):
                                frame = bytes(packet[packet[0] :])
                                if len(frame) == self.expected:
                                    self.received += 1
                                    if self.frames.full():
                                        self.frames.get_nowait()
                                        self.skipped += 1
                                    self.frames.put_nowait(frame)
                                else:
                                    self.invalid += 1
                            else:
                                self.invalid += 1
                            packet.clear()
                        if len(packet) > 32 * 1024 * 1024:
                            packet.clear()
                            self.invalid += 1
                    elif status not in (
                        usb1.TRANSFER_CANCELLED,
                        usb1.TRANSFER_TIMED_OUT,
                    ):
                        self.error = f"Camera USB transfer status {status}"
                        self.stop_event.set()
                    if not self.stop_event.is_set():
                        transfer.submit()

                for _ in range(self.queue_depth):
                    transfer = handle.getTransfer()
                    transfer.setBulk(
                        0x81, self.chunk_size, callback=callback, timeout=1500
                    )
                    transfers.append(transfer)
                    transfer.submit()
                self.ready.set()
                while not self.stop_event.is_set():
                    context.handleEventsTimeout(0.02)
            except Exception as error:
                self.error = str(error)
                self.ready.set()
            finally:
                self.stop_event.set()
                for transfer in transfers:
                    if transfer.isSubmitted():
                        try:
                            transfer.cancel()
                        except usb1.USBErrorNotFound:
                            pass
                until = time.monotonic() + 2
                while (
                    any(t.isSubmitted() for t in transfers) and time.monotonic() < until
                ):
                    context.handleEventsTimeout(0.02)
                if handle:
                    handle.releaseInterface(1)
                    handle.close()

    def read(self, timeout=0.1):
        if self.error:
            raise RuntimeError(self.error)
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def stats(self):
        return {
            "received": self.received,
            "preview_skipped": self.skipped,
            "invalid": self.invalid,
            "bytes": self.bytes_received,
            "elapsed": time.monotonic() - self.started,
        }

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=4)
        if self.thread.is_alive():
            raise RuntimeError("Camera USB worker did not stop")
