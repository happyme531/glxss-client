"""Protocol regression tests for the mistakes that originally blocked the camera."""

import struct
import unittest
from unittest.mock import patch

from PIL import Image

from glxss import Glasses, decode_sensor, text_image, yv12_image


class FakeUSB:
    def __init__(self, reads=()):
        self.reads = iter(reads)
        self.controls = []

    def read(self, *args, **kwargs):
        return next(self.reads)

    def ctrl_transfer(self, *args, **kwargs):
        self.controls.append(args)
        return len(args[4]) if not isinstance(args[4], int) else bytes(args[4])


def glasses(device):
    with patch("glxss.find_device", return_value=device):
        result = Glasses()
    result.claimed.update((0, 1, 2, 3))
    return result


class ProtocolTests(unittest.TestCase):
    def test_sensor_signed_axes_and_units(self):
        raw = struct.pack(
            "<BBH9hBBf",
            1,
            113,
            123,
            -3,
            4,
            -5,
            -8192,
            0,
            8192,
            -16384,
            0,
            16384,
            2,
            1,
            42.5,
        )
        sample = decode_sensor(raw)
        self.assertEqual(sample["compass_raw"], [-3, 4, -5])
        self.assertEqual(sample["acc_g"], [-1, 0, 1])
        self.assertEqual(sample["gyro_dps"], [-1000, 0, 1000])
        self.assertEqual(sample["temperature_c"], 42.5)
        self.assertEqual(sample["key_code"], 113)

    def test_camera_switch_targets_extension_unit_four(self):
        device = FakeUSB()
        glasses(device).camera_control(0x0D0D, b"\x01")
        self.assertEqual(device.controls, [(0x21, 1, 0x0D0D, 0x0400, b"\x01")])

    def test_bulk_continuation_has_no_extra_header(self):
        # Firmware sets EOF on the first chunk, and continues the same payload.
        width, height = 640, 480
        frame = bytes([80]) * (width * height) + bytes([128]) * (width * height // 2)
        payload = bytes([12, 15]) + bytes(10) + frame
        chunks = [payload[i : i + 65536] for i in range(0, len(payload), 65536)]
        client = glasses(FakeUSB(chunks))
        client.camera_running = True
        client.width, client.height = width, height
        for _ in chunks[:-1]:
            self.assertIsNone(client.camera_read())
        image = client.camera_read()
        self.assertEqual(image.size, (640, 480))
        self.assertLess(max(image.getpixel((0, 0))) - min(image.getpixel((0, 0))), 2)

    def test_stale_resolution_is_discarded(self):
        client = glasses(FakeUSB([bytes([12, 15]) + bytes(10) + bytes(100)]))
        client.camera_running = True
        client.width, client.height = 640, 480
        self.assertIsNone(client.camera_read())
        self.assertEqual(client.discarded_frames, 1)

    def test_yv12_v_u_order_and_limited_range(self):
        image = yv12_image(bytes([16]) * 4 + bytes([128, 128]), 2, 2)
        self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))
        white = yv12_image(bytes([235]) * 4 + bytes([128, 128]), 2, 2)
        self.assertEqual(white.getpixel((0, 0)), (255, 255, 255))
        red = yv12_image(bytes([81]) * 4 + bytes([240, 90]), 2, 2)
        r, g, b = red.getpixel((0, 0))
        self.assertGreater(r, 245)
        self.assertLess(max(g, b), 8)

    def test_bad_reports_are_rejected(self):
        with self.assertRaises(ValueError):
            decode_sensor(bytes(27))
        with self.assertRaises(ValueError):
            yv12_image(bytes(10), 640, 480)

    def test_worn_pitch_is_positive_up_negative_down(self):
        # Physical observation: looking up gives +X, looking down gives -X.
        up = struct.pack(
            "<BBH9hBBf", 0, 113, 0, 0, 0, 0, 4096, 7094, 0, 0, 0, 0, 0, 0, 25
        )
        down = struct.pack(
            "<BBH9hBBf", 0, 113, 0, 0, 0, 0, -4096, 7094, 0, 0, 0, 0, 0, 0, 25
        )
        self.assertAlmostEqual(decode_sensor(up)["pitch_deg"], 30, delta=0.01)
        self.assertAlmostEqual(decode_sensor(down)["pitch_deg"], -30, delta=0.01)

    def test_text_card_supports_chinese(self):
        image = text_image("你好，GLXSS！\n屏幕投送测试")
        self.assertEqual(image.size, (640, 400))
        self.assertIsInstance(image, Image.Image)


if __name__ == "__main__":
    unittest.main()
