"""Order playback against confirmed camera suspension, without blocking the GUI."""


class PlaybackGuard:
    def __init__(self, video_send, audio_send, enabled=True):
        self.video_send, self.audio_send = video_send, audio_send
        self.enabled = enabled
        self.video_available = False
        self.held = False
        self.hold_pending = None
        self.sequence = 0
        self.current = None
        self.pending = None

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.advance()

    def set_video_available(self, available):
        self.video_available = available
        if not available:
            self.held = False
            self.hold_pending = None
        self.advance()

    def play(self, op, **data):
        self.sequence += 1
        self.current = self.sequence
        self.pending = (op, dict(data, request_id=self.current))
        self.advance()

    def stop(self):
        if self.pending is not None:
            self.pending = None
            self.current = None
            self.advance()
        elif self.current is not None:
            self.audio_send("play_stop", request_id=self.current)

    def advance(self):
        required = self.enabled and self.current is not None and self.video_available
        if self.hold_pending is not None:
            return
        if required != self.held:
            self.hold_pending = required
            self.video_send("audio_hold", enabled=required)
            return
        if self.pending is not None:
            op, data = self.pending
            self.pending = None
            self.audio_send(op, **data)

    def acknowledge(self, enabled):
        if self.hold_pending is None or self.hold_pending != enabled:
            return
        self.held, self.hold_pending = enabled, None
        self.advance()

    def complete(self, request_id):
        # Replacing a playing file emits an end event for the old file first.
        # It must not release the camera while the replacement is starting.
        if request_id != self.current:
            return
        self.current = self.pending = None
        self.advance()

    def audio_disconnected(self):
        self.current = self.pending = None
        self.advance()

    def suspension_failed(self):
        # Do not start pending audio if we could not confirm that video stopped.
        self.pending = self.current = None
        self.video_available = False
        self.held = False
        self.hold_pending = None

    def close(self):
        self.current = self.pending = None
        self.video_available = False
        self.held = False
        self.hold_pending = None
