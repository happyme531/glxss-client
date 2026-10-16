import multiprocessing
import time
import unittest

from frame_transport import PreviewBuffer
from video import FrameConverter


def write_frames(path):
    with PreviewBuffer(path) as writer:
        for i in range(1, 81):
            writer.write(bytes([i]) * (320 * 180 * 4), 320, 180, 1920, 1080, i)


class VideoTests(unittest.TestCase):
    def test_yv12_plane_order_black_white_and_scaled_dimensions(self):
        with FrameConverter() as converter:
            for luma, expected in ((16, 0), (235, 255)):
                image = converter.image(bytes([luma]) * 64 + bytes([128]) * 32, 8, 8)
                # FFmpeg's fixed-point RGB conversion can round white to 253.
                self.assertTrue(
                    all(abs(v - expected) <= 2 for v in image.getpixel((0, 0)))
                )
            red = bytes([81]) * 64 + bytes([240]) * 16 + bytes([90]) * 16
            image = converter.image(red, 8, 8, (4, 4))
            self.assertEqual(image.size, (4, 4))
            r, g, b = image.getpixel((1, 1))
            self.assertGreater(r, 245)
            self.assertLess(max(g, b), 8)
            with self.assertRaises(ValueError):
                converter.image(red[:-1], 8, 8)

    def test_portable_fallback_without_ffmpeg(self):
        with FrameConverter() as converter:
            converter.library = None
            self.assertEqual(converter.backend, "Pillow")
            red = bytes([81]) * 64 + bytes([240]) * 16 + bytes([90]) * 16
            image = converter.image(red, 8, 8, (4, 4))
            self.assertEqual(image.size, (4, 4))
            r, g, b = image.getpixel((1, 1))
            self.assertGreater(r, 245)
            self.assertLess(max(g, b), 8)

    def test_concurrent_transport_never_returns_torn_frames(self):
        with PreviewBuffer() as reader:
            child = multiprocessing.Process(target=write_frames, args=(reader.path,))
            child.start()
            previous = 0
            deadline = time.monotonic() + 5
            while child.is_alive() and time.monotonic() < deadline:
                frame = reader.read(previous)
                if frame:
                    self.assertEqual(
                        frame["pixels"],
                        bytes([int(frame["timestamp"])]) * (320 * 180 * 4),
                    )
                    self.assertEqual(
                        (frame["source_width"], frame["source_height"]), (1920, 1080)
                    )
                    previous = frame["sequence"]
            child.join(2)
            self.assertEqual(child.exitcode, 0)
            frame = reader.read()
            self.assertEqual(frame["sequence"], 80)
            self.assertIsNone(reader.read(80))
            # Reconnecting a worker continues the sequence for the same GUI.
            with PreviewBuffer(reader.path) as replacement:
                self.assertEqual(replacement.write(bytes(16), 2, 2, 640, 480, 81), 81)
            self.assertEqual(reader.read(80)["sequence"], 81)


if __name__ == "__main__":
    unittest.main()
