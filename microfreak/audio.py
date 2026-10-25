"""Audio input and output: find an interface, record from it, play a file.

`render` and `speech preview` record through this, and microfreak-emu's
filter_cal rig does too. It uses sounddevice, a PortAudio binding, so the
same code runs on macOS, Windows and Linux.

sounddevice is imported inside `_sd()`, not here, so a machine without
PortAudio still runs every command that does not touch audio.
"""
import sys
import threading
import time
import wave

import numpy as np

DEFAULT_RATE = 48000

# How long a stream may take to deliver its first block before the device is
# treated as dead. Core Audio opens in well under a second.
OPEN_TIMEOUT = 3.0

# Gaps within this are clock drift between the audio clock and the host
# clock, not dropped audio: 4 ms plus 100 ppm of the length. A USB codec here
# measured 10 ppm. The shortest real loss, one 256-frame block at 48 kHz, is
# 5.3 ms.
DROP_TOLERANCE = 0.004
DRIFT_PPM = 100


class AudioError(Exception):
    """Raised for anything the user can fix: no device, no PortAudio."""


def _sd():
    try:
        import sounddevice
    except ImportError:
        raise AudioError(
            "sounddevice is not installed in %s. If mfreak was installed "
            "with `uv tool install`, reinstall it: uv tool install "
            "--editable --reinstall <microfreak-tools checkout>" % sys.prefix)
    except OSError as exc:
        raise AudioError("PortAudio is missing (%s). On Linux, install "
                         "libportaudio2." % exc)
    return sounddevice


def inputs():
    """-> [(index, name, channels, default_rate)] for each device with inputs."""
    sd = _sd()
    return [(i, d["name"], d["max_input_channels"], int(d["default_samplerate"]))
            for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0]


def find_input(spec=None):
    """-> (index, name, channels) for `spec`.

    None is the system default input. A number is a device index. Anything
    else matches a device name, ignoring case; an exact name wins over a
    partial one.
    """
    devs = inputs()
    if not devs:
        raise AudioError("no audio inputs on this machine")
    if spec is None:
        idx = _sd().default.device[0]
        found = [d for d in devs if d[0] == idx]
        if not found:
            raise AudioError("no default audio input. Pass --device; "
                             "--list-devices shows them.")
    elif str(spec).isdigit():
        found = [d for d in devs if d[0] == int(spec)]
        if not found:
            raise AudioError("no audio input %s. --list-devices shows them."
                             % spec)
    else:
        want = str(spec).lower()
        found = ([d for d in devs if d[1].lower() == want]
                 or [d for d in devs if want in d[1].lower()])
        if not found:
            raise AudioError("no audio input matching %r. Inputs:\n%s"
                             % (spec, describe(devs)))
        if len(found) > 1:
            raise AudioError("%r matches %d inputs. Pass a number:\n%s"
                             % (spec, len(found), describe(found)))
    idx, name, channels, _rate = found[0]
    return idx, name, channels


def describe(devs=None):
    """The input list as text, one device per line."""
    devs = inputs() if devs is None else devs
    return "\n".join("  %2d  %-40s %d in, %d Hz" % d for d in devs)


class Recorder:
    """Record from an input in the background. Use it as a context manager.

        with Recorder("USB Audio CODEC") as rec:
            start = rec.mark()
            ...play something...
        x = rec.audio()[start:]

    `mark()` returns the frame index of the current moment, so a caller can
    cut the recording at the instant it started playing. `dropped()` reports
    audio lost on the way, whether or not PortAudio flagged it, and `gaps`
    says where.
    """

    def __init__(self, device=None, rate=DEFAULT_RATE, channels=None):
        sd = _sd()
        self.index, self.name, available = find_input(device)
        self.channels = min(available, 2) if channels is None else channels
        self.rate = rate
        self.overflows = 0
        self.gaps = []                # (seconds into the recording, seconds missing)
        self._lost = 0.0              # frames already counted in self.gaps
        self._blocks = []
        self._frames = 0
        self._first_adc = None
        self._last = None             # (adc time, frame index, frames) of the newest block
        self._lock = threading.Lock()
        self._ready = threading.Event()
        try:
            self._stream = sd.InputStream(
                device=self.index, channels=self.channels, samplerate=rate,
                dtype="float32", callback=self._callback)
        except Exception as exc:
            raise AudioError("could not open %s at %d Hz, %d channel(s): %s"
                             % (self.name, rate, self.channels, exc))

    def _callback(self, indata, frames, time_info, status):
        if status.input_overflow:
            self.overflows += 1
        # Some host APIs report an ADC time of 0. The callback time less the
        # block's length is the next best estimate of when it was captured.
        adc = time_info.inputBufferAdcTime or (time_info.currentTime
                                               - frames / self.rate)
        with self._lock:
            if self._first_adc is None:
                self._first_adc = adc
            missing = ((adc - self._first_adc) * self.rate - self._frames
                       - self._lost)
            if missing > DROP_TOLERANCE * self.rate:
                self.gaps.append((self._frames / self.rate,
                                  missing / self.rate))
                self._lost += missing
            self._last = (adc, self._frames, frames)
            self._blocks.append(indata.copy())
            self._frames += frames
        self._ready.set()

    def __enter__(self):
        self._stream.start()
        if not self._ready.wait(OPEN_TIMEOUT):
            self._stream.close()
            raise AudioError("no audio arrived from %s within %.0f s"
                             % (self.name, OPEN_TIMEOUT))
        return self

    def __exit__(self, *exc):
        self._stream.stop()
        self._stream.close()
        return False

    def mark(self):
        """-> the frame index that corresponds to now."""
        now = self._stream.time
        with self._lock:
            adc, frame, _ = self._last
        return max(0, frame + int(round((now - adc) * self.rate)))

    def wait_for(self, frame, timeout=1.0):
        """Wait until audio up to `frame` has arrived, or `timeout` passes.

        The newest audio is still in the driver's buffers when the caller
        stops, so stopping at once would cut the end short.
        """
        deadline = time.monotonic() + timeout
        while self._frames < frame and time.monotonic() < deadline:
            time.sleep(0.005)

    def audio(self):
        """-> float32 array, (frames, channels)."""
        with self._lock:
            if not self._blocks:
                return np.zeros((0, self.channels), np.float32)
            return np.concatenate(self._blocks)

    def dropped(self):
        """-> seconds of audio that the clock says should be here and is not.

        Measured up to the newest block received, so audio still in the
        driver when the stream stopped does not count as lost.
        """
        adc, frame, frames = self._last
        expected = (adc - self._first_adc) * self.rate + frames
        missing = (expected - self._frames) / self.rate
        allowed = DROP_TOLERANCE + DRIFT_PPM * 1e-6 * self._frames / self.rate
        return missing if missing > allowed else 0.0


def mono(x):
    """Fold a two-channel recording to one channel, polarity-aware.

    The MicroFreak's output is mono. Through a TS cable into a stereo or
    differential input it arrives on both channels in opposite polarity, and
    averaging them cancels it to the noise floor. So the channels are
    subtracted when they are anti-phase and averaged when they are not.
    """
    if x.ndim == 1:
        return x
    if x.shape[1] == 1:
        return x[:, 0]
    L, R = x[:, 0], x[:, 1]
    if L.std() > 1e-9 and R.std() > 1e-9 and np.corrcoef(L, R)[0, 1] < -0.5:
        return (L - R) / 2.0
    return x.mean(axis=1)


def onset(x, rate, threshold=0.1):
    """-> seconds to the first sample above `threshold` of the peak, or None."""
    a = np.abs(x if x.ndim == 1 else x.max(axis=1))
    peak = float(a.max()) if a.size else 0.0
    if peak <= 0:
        return None
    return int(np.argmax(a >= threshold * peak)) / float(rate)


def levels(x):
    """-> (peak dBFS, clipped samples). Peak is -inf for silence."""
    peak = float(np.abs(x).max()) if x.size else 0.0
    db = 20 * np.log10(peak) if peak > 0 else float("-inf")
    return db, int((np.abs(x) >= 0.999).sum())


def write_wav(path, x, rate):
    """Write 16-bit PCM. `x` is float, (frames,) or (frames, channels)."""
    x = np.asarray(x, np.float32)
    channels = 1 if x.ndim == 1 else x.shape[1]
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def read_wav(path):
    """-> (float32 array, rate) from a 16-bit PCM WAV."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("%s is not 16-bit PCM" % path)
        raw = w.readframes(w.getnframes())
        channels, rate = w.getnchannels(), w.getframerate()
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    return (x if channels == 1 else x.reshape(-1, channels)), rate


def play(x, rate):
    """Play `x` on the default output and wait for it to finish."""
    sd = _sd()
    sd.play(x, rate)
    sd.wait()


def record(seconds, device=None, rate=DEFAULT_RATE, channels=None):
    """-> (audio, recorder): `seconds` of audio from `device`."""
    with Recorder(device, rate, channels) as rec:
        start = rec.mark()
        time.sleep(seconds)
    return rec.audio()[start:start + int(seconds * rate)], rec
