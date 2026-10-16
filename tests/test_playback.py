import struct
import unittest

from PIL import Image

from lcd_stream import lcd_packet
from playback import PlaybackGuard


class PlaybackGuardTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.guard = PlaybackGuard(
            lambda op, **data: self.calls.append(("video", op, data)),
            lambda op, **data: self.calls.append(("audio", op, data)),
        )
        self.guard.set_video_available(True)

    def test_play_waits_for_stop_confirmation_and_restores_on_finish(self):
        self.guard.play("tone")
        self.assertEqual(self.calls, [("video", "audio_hold", {"enabled": True})])
        self.guard.acknowledge(True)
        self.assertEqual(self.calls[-1], ("audio", "tone", {"request_id": 1}))
        self.guard.complete(1)
        self.assertEqual(self.calls[-1], ("video", "audio_hold", {"enabled": False}))

    def test_replacement_ignores_old_end_event(self):
        self.guard.play("tone")
        self.guard.acknowledge(True)
        self.guard.play("play", path="replacement.wav")
        self.guard.complete(1)
        self.assertEqual(self.guard.current, 2)
        self.assertEqual(self.calls[-1][1], "play")
        self.guard.complete(2)
        self.assertEqual(self.calls[-1], ("video", "audio_hold", {"enabled": False}))

    def test_cancel_before_confirmation_never_starts_audio(self):
        self.guard.play("tone")
        self.guard.stop()
        self.guard.acknowledge(True)
        self.assertEqual([call[0] for call in self.calls], ["video", "video"])
        self.assertFalse(self.calls[-1][2]["enabled"])

    def test_disable_and_enable_during_playback(self):
        self.guard.play("tone")
        self.guard.acknowledge(True)
        self.guard.set_enabled(False)
        self.guard.acknowledge(False)
        self.guard.set_enabled(True)
        self.assertTrue(self.calls[-1][2]["enabled"])
        self.guard.acknowledge(True)
        self.guard.audio_disconnected()
        self.assertFalse(self.calls[-1][2]["enabled"])

    def test_disabled_option_sends_audio_immediately(self):
        self.guard.set_enabled(False)
        self.guard.play("tone")
        self.assertEqual(self.calls, [("audio", "tone", {"request_id": 1})])

    def test_stop_waits_until_audio_worker_has_drained_transfers(self):
        self.guard.play("tone")
        self.guard.acknowledge(True)
        self.guard.stop()
        self.assertEqual(self.calls[-1], ("audio", "play_stop", {"request_id": 1}))
        self.assertTrue(self.guard.held)
        self.guard.complete(1)
        self.assertFalse(self.calls[-1][2]["enabled"])

    def test_failed_suspension_cancels_waiting_playback(self):
        self.guard.play("tone")
        self.guard.suspension_failed()
        self.assertIsNone(self.guard.pending)
        self.assertIsNone(self.guard.current)
        self.assertEqual(len(self.calls), 1)

    def test_new_playback_waits_for_previous_resume_and_new_suspend(self):
        self.guard.play("tone")
        self.guard.acknowledge(True)
        self.guard.complete(1)
        self.guard.play("tone")
        self.guard.acknowledge(False)
        self.assertEqual(self.calls[-1], ("video", "audio_hold", {"enabled": True}))
        self.guard.acknowledge(True)
        self.assertEqual(self.calls[-1], ("audio", "tone", {"request_id": 2}))


class LCDPacketTests(unittest.TestCase):
    def test_header_rgba_and_black_letterbox(self):
        packet, canvas = lcd_packet(Image.new("RGB", (4, 2), "red"), 8, 8)
        self.assertEqual(struct.unpack("<IIB7x", packet[:16]), (8, 8, 1))
        self.assertEqual(len(packet), 16 + 8 * 8 * 4)
        self.assertEqual(packet[16:20], bytes((0, 0, 0, 255)))
        self.assertEqual(canvas.getpixel((4, 4)), (255, 0, 0))
        self.assertEqual(canvas.getpixel((4, 7)), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
