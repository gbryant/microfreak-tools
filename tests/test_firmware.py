#!/usr/bin/env python3
"""Firmware files: reading every shape, repacking, planning and patching.

    uv run python tests/test_firmware.py
    MFREAK_FIRMWARE=path/to/stock.mff uv run python tests/test_firmware.py

Needs Arturia's stock 5.0.0.2084 .mff, which is not in this repository. It
looks in $MFREAK_FIRMWARE, then in a microfreak-emu checkout next to this one,
and skips if there is none. No instrument. A few seconds.
"""
import contextlib
import glob
import io
import os
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from microfreak.firmware import flash as F                     # noqa: E402
from microfreak.firmware import image as I                     # noqa: E402
from microfreak.firmware import mff                            # noqa: E402
from microfreak.firmware import patching as X                  # noqa: E402

fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-56s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)


def stock():
    env = os.environ.get("MFREAK_FIRMWARE")
    if env:
        return env
    found = glob.glob(os.path.join(os.path.dirname(ROOT), "microfreak-emu",
                                   "data", "firmware", "*5_0_0_2084*.mff"))
    return found[0] if found else None


def mfreak(*args):
    return subprocess.run(
        [sys.executable, "-c", "import sys; from microfreak.cli import main; "
                               "sys.exit(main(sys.argv[1:]))", *args],
        cwd=ROOT, capture_output=True, text=True)


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = fn(*args)
    return rc, out.getvalue()


def buzz(path, seconds, f0=120.0):
    """A voiced test sound: harmonics of f0 with a slow vowel-like sweep."""
    import wave
    import numpy as np
    rate = 16000
    t = np.arange(int(seconds * rate)) / rate
    x = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 20))
    x *= 0.5 + 0.5 * np.sin(2 * np.pi * 1.5 * t) ** 2
    pcm = (x / np.max(np.abs(x)) * 20000).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


def test_embed(stock_path, work, stock_code, speech_path):
    print("\nspeech embed")
    a, b, c = (buzz(os.path.join(work, n + ".wav"), s)
               for n, s in (("a", 0.6), ("b", 0.9), ("c", 0.5)))
    words = os.path.join(work, "words.mff")
    r = mfreak("speech", "embed", stock_path, "--bank", a, b, "--bank", c,
               "--out", words)
    chk(r.returncode == 0 and os.path.isfile(words), "embeds words into stock",
        r.stderr.strip()[-80:])
    if r.returncode:
        return
    got = I.main_code(words)
    banks = X.word_banks(got)
    chk(banks[0] != banks[1] and banks[2] == banks[3] == banks[4] == banks[1],
        "  banks 1 and 2 hold words, 3 to 5 repeat bank 2")
    chk(X.banner(got) == "PATCHED words2" and X.answers(got) == ("W", 2)
        and X.identity(got) is None,
        "  it names itself words2, answers opcode 0x48 as W, and the Device "
        "Inquiry as stock")
    chk(len(got) == len(stock_code), "  the main image keeps its size")
    if len(got) != len(stock_code):
        return
    lo, hi = X.VOCAB_START - X.BASE, X.REGION_END - X.BASE
    tab = X.FLASH_DESC - X.BASE
    tbh = X.TBH_TABLE - X.BASE
    ver = X.VERSION_STR - X.BASE
    outside = [i for i in range(len(stock_code)) if got[i] != stock_code[i]
               and not (lo <= i < hi or tab <= i < tab + 40
                        or tbh <= i < tbh + 2 * (X.TBH_MAX_INDEX + 1)
                        or ver <= i < ver + X.VERSION_SLOT)]
    chk(not outside, "  nothing changes outside the banks, table, banner and dispatch",
        "%d bytes" % len(outside) if outside else "")
    chk([b for _, _, b in I.read(words)[1][1:]]
        == [b for _, _, b in I.read(stock_path)[1][1:]],
        "  the other four images are unchanged")
    r = mfreak("fw", "info", words)
    chk("bank 1   2 words" in r.stdout and "bank 3  repeats bank 2" in r.stdout,
        "  fw info lists the banks")

    again = os.path.join(work, "again.mff")
    r = mfreak("speech", "embed", words, "--bank", c, "--out", again)
    got = I.main_code(again) if r.returncode == 0 else None
    chk(got is not None and len(set(X.word_banks(got))) == 1
        and X.banner(got) == "PATCHED words2" and X.answers(got) == ("W", 2),
        "re-embedding wipes the earlier words")

    if os.path.isfile(speech_path):
        sw = os.path.join(work, "speech-words.mff")
        r = mfreak("speech", "embed", speech_path, "--bank", a, "--out", sw)
        before, got = I.main_code(speech_path), I.main_code(sw) if r.returncode == 0 else None
        cb = X.CODE_BASE - X.BASE
        chk(got is not None and got[cb:hi] == before[cb:hi]
            and X.banner(got) == X.banner(before) and X.answers(got) == X.answers(before),
            "embedding into speech keeps its code, banner and identity")

    vocab = os.path.join(work, "vocab.txt")
    open(vocab, "w").write("# test\n[2]\n%s\n\n[5]\n%s\n%s\n"
                           % (os.path.basename(a), os.path.basename(b), c))
    out = os.path.join(work, "fromfile.mff")
    r = mfreak("speech", "embed", stock_path, "--from", vocab, "--out", out)
    got = I.main_code(out) if r.returncode == 0 else None
    wb = X.word_banks(got) if got else []
    chk(got is not None and wb[0] == wb[1] == wb[2] == wb[3] != wb[4],
        "--from puts words where the headings say", r.stderr.strip()[-80:])
    for text, want in (("[6]\nx\n", "not a Wave position"),
                       ("x\n[1]\ny\n", "before the first heading"),
                       ("[2]\nx\n[2]\ny\n", "appears twice"),
                       ("[1]\n" + "x\n" * 32, "more than 31 words")):
        open(vocab, "w").write(text)
        r = mfreak("speech", "embed", stock_path, "--from", vocab, "--dry-run")
        chk(r.returncode != 0 and want in r.stderr, "  refuses a file: %s" % want)

    long_ = buzz(os.path.join(work, "long.wav"), 26.0)
    r = mfreak("speech", "embed", stock_path, "--bank", long_, "--dry-run")
    chk(r.returncode != 0 and "limit is 25.6 s per bank" in r.stderr,
        "refuses a bank over the time limit")
    full = buzz(os.path.join(work, "full.wav"), 20.0)
    over = os.path.join(work, "over.mff")
    r = mfreak("speech", "embed", stock_path, *sum(
        (["--bank", full] for _ in range(5)), []), "--out", over)
    chk(r.returncode != 0 and "will not fit" in r.stderr and not os.path.exists(over),
        "refuses words that do not fit, and writes nothing", r.stderr.strip()[-70:])
    bad = os.path.join(work, "bad.raw")
    changed = bytearray(stock_code)
    changed[0x1000] ^= 0xFF
    open(bad, "wb").write(bytes(changed))
    r = mfreak("speech", "embed", bad, "--bank", a, "--dry-run")
    chk(r.returncode != 0 and "refusing" in r.stderr, "refuses a file it does not know")


def main():
    path = stock()
    if not path or not os.path.isfile(path):
        print("SKIP - no stock 5.0.0.2084 .mff (set MFREAK_FIRMWARE)")
        return 0
    work = tempfile.mkdtemp()

    print("\nreading every shape of a firmware file")
    meta, images = I.read(path)
    chk([n for n, _, _ in images] == [0, 1, 2, 3, 4], "the stock .mff holds five images")
    code = I.main_code(path)
    chk(X.payload_sha256(code) == X.KNOWN_PAYLOAD_SHA256,
        "its main code is the build `fw patch` knows")
    member = os.path.join(work, "main.bin")
    open(member, "wb").write(images[0][2])
    raw = os.path.join(work, "main.raw")
    open(raw, "wb").write(bytes(code))
    chk(I.main_code(member) == code and I.main_code(raw) == code,
        "the main image and its code alone read the same")
    chk(I.package(code, 0) == images[0][2], "packaging the code rebuilds the image")

    print("\nrepack")
    same = os.path.join(work, "same.mff")
    quiet(mff.repack, path, same, [], None)
    chk(open(same, "rb").read() == open(path, "rb").read(),
        "unchanged, it is byte-identical to Arturia's")
    replaced = os.path.join(work, "replaced.mff")
    quiet(mff.repack, path, replaced, ["main=%s" % raw], None)
    chk(open(replaced, "rb").read() == open(path, "rb").read(),
        "replacing main with its own code changes nothing")
    only = os.path.join(work, "only.mff")
    quiet(mff.repack, path, only, [], [0])
    chk([n for n, _, _ in I.read(only)[1]] == [0], "--only main keeps one image")
    one = os.path.join(work, "one.mff")
    quiet(mff.repack, member, one, [], None)
    chk(I.read(one)[1][0][2] == images[0][2], "one packaged image becomes a .mff")

    print("\nflash plans what it would send")
    every = F.plan(path, None)
    sent = sum(len(d) for *_, d in every if d)
    chk(sent == sum(len(b) for _, _, b in images),
        "by default, every image in the file", "%d bytes" % sent)
    chk(sum(len(d) for *_, d in F.plan(path, {0}) if d) == len(images[0][2]),
        "--only main, the main image alone")
    r = mfreak("fw", "flash", path)
    chk(r.returncode == 0 and "dry run" in r.stderr and "5 images" in r.stdout,
        "without a terminal it lists five images and writes nothing")

    print("\npatch and identify")
    for src in (path, member, raw):
        out = os.path.join(work, "speech.mff")
        r = mfreak("fw", "patch", "speech", src, "--out", out)
        ok = r.returncode == 0 and os.path.isfile(out)
        if ok:
            ok = mff.identify(I.main_code(out)).startswith("PATCHED speech")
        chk(ok, "fw patch takes %s" % os.path.basename(src))
        if ok:
            got = I.read(out)[1]
            want = images if src == path else images[:1]
            chk([n for n, _, _ in got] == [n for n, _, _ in want]
                and [b for _, _, b in got[1:]] == [b for _, _, b in want[1:]],
                "  and keeps %s" % ("the other four images unchanged"
                                    if src == path else "the main image alone"))
    peek = os.path.join(work, "peek.mff")
    r = mfreak("fw", "patch", "peek", path, "--out", peek)
    chk(r.returncode == 0
        and mff.identify(I.main_code(peek)).startswith("PATCHED peek"),
        "fw patch peek builds, and fw info names it")
    chk(mff.identify(code).startswith("stock 5.0.0.2084"), "fw info calls stock stock")
    from microfreak import plugin
    want = {n: p.firmware.identity for n, p in plugin.firmwares().items()}
    got = {}
    for n, f in (("speech", os.path.join(work, "speech.mff")), ("peek", peek)):
        c = I.main_code(f)
        got[n] = X.answers(c)
        chk(got[n] is not None and got[n] == (want.get(n), X.banner_rev(c))
            and X.identity(c) is None,
            "%s answers opcode 0x48 with its letter and rev, and the Device "
            "Inquiry as stock" % n, "%s, rev %d" % got[n] if got[n] else "")
    chk(X.answers(code) is None, "stock does not answer opcode 0x48")
    r = mfreak("fw", "patch", "speech", os.path.join(work, "speech.mff"),
               "--out", os.path.join(work, "twice.mff"))
    chk(r.returncode != 0 and "already patched" in r.stderr,
        "fw patch refuses its own output")
    r = mfreak("fw", "patch", "--list")
    chk(r.returncode == 0 and "speech" in r.stdout and "peek" in r.stdout,
        "fw patch --list names both first-party patches")

    test_embed(path, work, code, os.path.join(work, "speech.mff"))

    if fails:
        print("\nFAIL - %d check(s) failed" % len(fails))
        return 1
    print("\nPASS - firmware files read, repack, plan and patch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
