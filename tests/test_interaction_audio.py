import math
import tempfile
import unittest
from pathlib import Path

from audio import pcm_levels, quantized_volume, tone_pcm
from interaction import ButtonTracker, CompassCalibration, magnetic_heading


class InteractionTests(unittest.TestCase):
    def test_physical_button_edges_and_short_long_press(self):
        button = ButtonTracker()
        self.assertIsNone(button.update(0x71, 0, 0))
        self.assertEqual(button.update(0x71, 1, 1)["phase"], "press")
        self.assertIsNone(button.update(0x71, 1, 1.2))
        self.assertEqual(button.update(0x71, 0, 1.25)["gesture"], "short")
        self.assertIsNone(button.update(0x71, 0, 1.4))
        button.update(0x71, 1, 2)
        release = button.update(0x71, 0, 3)
        self.assertEqual(release["gesture"], "long")
        self.assertEqual(release["duration_ms"], 1000)
        self.assertEqual(release["presses"], 2)

    def test_initial_pressed_state_does_not_trigger_action_on_release(self):
        button = ButtonTracker()
        self.assertIsNone(button.update(0x71, 1, 0))
        self.assertIsNone(button.update(0x71, 0, 1))

    def test_heading_cardinal_directions_and_tilt(self):
        gravity = (0, 1, 0)
        self.assertAlmostEqual(magnetic_heading((0, 0, 100), gravity), 0)
        self.assertAlmostEqual(magnetic_heading((-100, 0, 0), gravity), 90)
        self.assertAlmostEqual(magnetic_heading((0, 0, -100), gravity), 180)
        self.assertAlmostEqual(magnetic_heading((100, 0, 0), gravity), 270)
        self.assertIsNone(magnetic_heading((0, 0, 0), gravity))
        self.assertIsNone(magnetic_heading((0, 0, 100), (0, 0, 1)))
        angle = math.radians(35)
        self.assertAlmostEqual(
            magnetic_heading(
                (0, -100 * math.sin(angle), 100 * math.cos(angle)),
                (0, math.cos(angle), math.sin(angle)),
            ),
            0,
        )

    def test_calibration_recovers_offset_and_axis_gain(self):
        calibration = CompassCalibration()
        calibration.start()
        for vector in (
            (1000, 0, 0),
            (-1000, 0, 0),
            (0, 2000, 0),
            (0, -2000, 0),
            (0, 0, 500),
            (0, 0, -500),
        ):
            for _ in range(10):
                calibration.add([vector[0] + 100, vector[1] - 200, vector[2] + 50])
        calibration.finish()
        self.assertEqual(calibration.offset, [100, -200, 50])
        self.assertTrue(calibration.valid)
        self.assertAlmostEqual(
            calibration.correct((1100, -200, 50))[0],
            calibration.correct((100, 1800, 50))[1],
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "compass.json"
            calibration.save(path)
            loaded = CompassCalibration()
            loaded.load(path)
            self.assertEqual(loaded.offset, calibration.offset)

    def test_partial_rotation_does_not_produce_a_false_calibration(self):
        calibration = CompassCalibration()
        calibration.start()
        for i in range(100):
            calibration.add((i, 10, 1))
        with self.assertRaises(ValueError):
            calibration.finish()


class AudioTests(unittest.TestCase):
    def test_pcm_tone_duration_channels_and_level(self):
        pcm = tone_pcm(0.1, frequency=500, level=0.1)
        self.assertEqual(len(pcm), 4410 * 4)
        rms, peak = pcm_levels(pcm)
        self.assertAlmostEqual(rms[0], rms[1])
        self.assertAlmostEqual(peak[0], -20, delta=0.1)
        self.assertLess(rms[0], peak[0])
        self.assertEqual(pcm_levels(bytes(440))[0], [-90.0, -90.0])

    def test_volume_quantizes_and_clamps_to_device_range(self):
        self.assertEqual(quantized_volume(-10, -16801, 0, 96), -16801)
        self.assertLessEqual(quantized_volume(110, -16801, 0, 96), 0)
        selected = quantized_volume(65, -16801, 0, 96)
        self.assertEqual((selected + 16801) % 96, 0)


if __name__ == "__main__":
    unittest.main()
