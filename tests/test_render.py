#!/usr/bin/env python3
"""`mfreak render` records the right span, folds to mono, and reports drops.

    uv run python tests/test_render.py

No instrument and no audio interface. A fake sounddevice delivers a tone
that starts when the fake synth receives a note-on. About 4 s.
"""
import os
import sys
import tempfile
import threading
import time
import types

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-52s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)


class World:
    """What the fake synth and the fake input share: is a note sounding?"""
    sounding = False
    drop_block = None          # index of a block the fake input never delivers
    overflow_block = None      # index of a block flagged as an overflow


class FakeSynth:
    port_name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(list(msg))
        kind = msg[0] & 0xF0
        if kind == 0x90 and len(msg) > 2 and msg[2] > 0:
            World.sounding = True
        elif kind == 0x80 or (kind == 0x90 and len(msg) > 2):
            World.sounding = False

    def close(self):
        pass


class FakeStream:
    """A real-time input stream: a 440 Hz tone, anti-phase on two channels,
    while a note is held, and silence otherwise."""
    BLOCK = 512

    def __init__(self, device, channels, samplerate, dtype, callback):
        self.channels, self.rate, self.callback = channels, samplerate, callback
        self._run = False
        self._t0 = None

    @property
    def time(self):
        return time.monotonic()

    def start(self):
        self._run = True
        self._t0 = time.monotonic()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        n = 0
        while self._run:
            adc = self._t0 + n * self.BLOCK / self.rate
            due = adc + self.BLOCK / self.rate
            time.sleep(max(0.0, due - time.monotonic()))
            t = (n * self.BLOCK + np.arange(self.BLOCK)) / self.rate
            tone = (0.5 * np.sin(2 * np.pi * 440 * t) if World.sounding
                    else np.zeros(self.BLOCK)).astype(np.float32)
            block = np.stack([tone, -tone], axis=1)[:, :self.channels]
            status = types.SimpleNamespace(input_overflow=n == World.overflow_block)
            info = types.SimpleNamespace(inputBufferAdcTime=adc, currentTime=due)
            if n != World.drop_block:
                self.callback(block, self.BLOCK, info, status)
            n += 1

    def stop(self):
        self._run = False

    def close(self):
        self._run = False


fake_sd = types.ModuleType("sounddevice")
fake_sd.query_devices = lambda: [
    dict(name="Built-in Mic", max_input_channels=1, default_samplerate=48000),
    dict(name="USB Audio CODEC", max_input_channels=2, default_samplerate=48000)]
fake_sd.default = types.SimpleNamespace(device=(0, 0))
fake_sd.InputStream = FakeStream
sys.modules["sounddevice"] = fake_sd

import microfreak.audio as audio          # noqa: E402
import microfreak.render as render        # noqa: E402

render.open_synth = lambda port: FakeSynth()
render.PRESET_SETTLE = 0.0
os.environ.pop("MFREAK_EMULATOR", None)


def run(*argv):
    sys.argv = ["mfreak render"] + list(argv)
    try:
        return render.main()
    except SystemExit as exc:
        return exc.code


def test_devices():
    print("\ndevices")
    chk(audio.find_input("usb audio")[1] == "USB Audio CODEC",
        "a partial name finds the device")
    chk(audio.find_input("1")[0] == 1, "a number is a device index")
    chk(audio.find_input(None)[0] == 0, "no name is the system default")
    try:
        audio.find_input("nothing like it")
        chk(False, "an unknown name is refused")
    except audio.AudioError:
        chk(True, "an unknown name is refused")


def test_render(work):
    print("\na clean render")
    out = os.path.join(work, "a.wav")
    code = run("--notes", "60:0.5", "--tail", "0.5", "--device", "USB",
               "--out", out)
    chk(code == 0, "exits 0", code)
    x, rate = audio.read_wav(out)
    chk(x.ndim == 1, "the output is mono")
    chk(len(x) == rate, "it is the notes plus the tail, exactly",
        "%d frames" % len(x))
    first = audio.onset(x, rate)
    chk(first is not None and first < 0.03,
        "the note starts at the start of the file",
        "%s ms" % (None if first is None else round(first * 1000)))
    sounding = np.abs(x[int(0.1 * rate):int(0.4 * rate)]).max()
    chk(sounding > 0.4, "anti-phase channels fold without cancelling",
        "peak %.2f" % sounding)
    chk(np.abs(x[int(0.6 * rate):]).max() < 1e-6, "the tail is silent")


def test_drops(work):
    print("\na render that loses audio")
    World.drop_block = 30
    out = os.path.join(work, "b.wav")
    code = run("--notes", "60:0.5", "--tail", "0.5", "--device", "USB",
               "--out", out)
    World.drop_block = None
    chk(code == 1, "a missing block exits 1", code)
    chk(os.path.exists(out), "and the file is still written")
    World.drop_block = 20
    with audio.Recorder("USB") as rec:
        time.sleep(0.4)
    World.drop_block = None
    block = FakeStream.BLOCK / 48000.0
    chk(len(rec.gaps) == 1 and abs(rec.gaps[0][0] - 20 * block) < 1e-6
        and abs(rec.gaps[0][1] - block) < 1e-6,
        "the gap is placed at the missing block", rec.gaps)
    World.overflow_block = 30
    code = run("--notes", "60:0.5", "--tail", "0.5", "--device", "USB",
               "--out", out)
    World.overflow_block = None
    chk(code == 1, "an overflow exits 1", code)


def test_refusals(work):
    print("\nrefused before anything opens")
    out = os.path.join(work, "c.wav")
    chk(run("--notes", "60", "--midi", "x.mid", "--out", out) == 2,
        "--notes with --midi")
    chk(run("--notes", "60") == 2, "no --out")
    chk(run("--preset", "0", "--out", out) == 2, "preset 0")
    os.environ["MFREAK_EMULATOR"] = "1"
    chk(run("--out", out) not in (0, None), "MFREAK_EMULATOR is refused")
    del os.environ["MFREAK_EMULATOR"]
    chk(not os.path.exists(out), "and none of them wrote a file")


def main():
    print(__doc__.splitlines()[0])
    with tempfile.TemporaryDirectory() as work:
        test_devices()
        test_render(work)
        test_drops(work)
        test_refusals(work)
    print("\n%s" % ("FAIL - %d check(s) failed" % len(fails) if fails
                    else "PASS - render records what it played"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
