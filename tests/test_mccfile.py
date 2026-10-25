#!/usr/bin/env python3
"""MCC files read and write, and convert to wire bytes and back.

    uv run python tests/test_mccfile.py

No instrument and no MCC install. Under a second.
"""
import io
import os
import sys
import tempfile
import zipfile
from contextlib import redirect_stderr

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import microfreak.bank as B                                    # noqa: E402
from microfreak import mccfile                                 # noqa: E402
from microfreak.sample.encode import pack_7to8                 # noqa: E402
from microfreak.wavetable.encode import encode_block           # noqa: E402

fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-56s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)


def tone(n, period=97.0, amp=12000):
    return np.round(amp * np.sin(2 * np.pi * np.arange(n) / period)).astype(np.int16)


def test_audio(work):
    print("\nsamples and wavetables")
    s16 = tone(mccfile.WAVETABLE_BYTES // 2)
    pcm = mccfile.pcm_from_s16(s16)
    frames = mccfile.wavetable_frames(pcm)
    old = [encode_block(s16[i * 2048:(i + 1) * 2048]) for i in range(4)]
    chk(frames == old, "wavetable frames match the block encoder")
    chk(mccfile.wavetable_pcm(frames) == pcm, "and decode back to the same PCM")

    pcm = mccfile.pcm_from_s16(tone(5000))
    wire = mccfile.sample_wire(pcm)
    chk(len(wire) == 3 * mccfile.WIRE_BLOCK, "a sample pads to whole blocks",
        "%d bytes" % len(wire))
    block = pcm[:4096] + bytes(20)
    chk(wire[:mccfile.WIRE_BLOCK] == pack_7to8(block),
        "each block is 4096 bytes plus 20 zeros, packed")
    chk(mccfile.sample_pcm(wire, len(pcm)) == pcm, "and decodes back")

    path = os.path.join(work, "t.mfsz")
    mccfile.write_one(path, mccfile.audio_archive("tone", pcm, "sample"), "sample")
    item = mccfile.read_one(path, "sample")
    chk(item.name == "tone" and item.pcm() == pcm, "a .mfsz round-trips name and audio")
    with zipfile.ZipFile(path) as z:
        chk(z.namelist() == ["0_tone"], "with MCC's one-member layout", z.namelist())


def test_preset(work):
    print("\npresets")
    wire = bytes(i % 128 for i in range(B.PRESET_WIRE_LEN))
    path = os.path.join(work, "p.mfpz")
    try:
        mccfile.preset_archive("x" * 15, 0, wire)
        chk(False, "a 15-character name is refused")
    except mccfile.FileError:
        chk(True, "a 15-character name is refused")
    ar = B.Archive(version="48", name="Bass One", tags=B.tag_string(),
                   values=list(wire), scalars=("3", "0", "0"), abc=("0", "0", "19"))
    mccfile.write_one(path, ar, "preset")
    name, category, got = mccfile.preset_of(mccfile.read_one(path, "preset"))
    chk((name, category, got) == ("Bass One", 3, wire),
        "a .mfpz round-trips name, category and payload")
    for bad, kind in (("p.bin", "preset"), ("p.mfsz", "preset"), ("w.mfpz", "wavetable")):
        try:
            mccfile.check_ext(bad, kind)
            chk(False, "%s is refused as a %s file" % (bad, kind))
        except mccfile.FileError:
            chk(True, "%s is refused as a %s file" % (bad, kind))
    try:
        mccfile.read_one(os.path.join(work, "none.mfpz"), "preset")
        chk(False, "a missing file is refused")
    except mccfile.FileError as exc:
        chk("no such file" in str(exc), "a missing file is refused", exc)


def test_banks(work):
    print("\nbanks")
    wire = bytes(B.PRESET_WIRE_LEN)
    empty = "22 serialization::archive 10 0 4 2 48 5 Bank1 1 0 0 18 %s 1 0 1 0\n" % ("0" * 18)

    def item(name):
        return B.build_archive(B.Archive("48", name, B.tag_string(), list(wire)))

    path = os.path.join(work, "two.mfprojz")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("P/.DS_Store", b"\x00\x00\x00\x01Bud1")
        z.writestr("P/01-One-A/01-One-A1.mbp", item("First"))
        z.writestr("P/01-One-A/02-One-A2.mbp", empty)
        z.writestr("P/02-Two-B/01-Two-B1.mbp", item("Other"))
    err = io.StringIO()
    with redirect_stderr(err):
        items = B.read_bank(path)
    chk([i.name for i in items if not i.is_empty()] == ["First"],
        "only the first bank is read, as MCC does")
    chk("2 banks" in err.getvalue(), "and the others are reported", err.getvalue().strip()[:60])
    chk(len(items) == 2, ".DS_Store is skipped", "%d items" % len(items))
    chk(B.build_archive(mccfile.empty_archive("Bank1")) == empty,
        "an empty slot is written as MCC writes it")

    from microfreak.preset.backup import save_project
    saved = os.path.join(work, "backup.mfprojz")
    used = bytes(i % 128 for i in range(B.PRESET_WIRE_LEN))
    bad = save_project(saved, {0: ("Lead", 2, used, 0x33), 1: (None, 0, None, None)})
    back = {i.slot: i for i in B.read_bank(saved)}
    chk(not bad and back[1].wire() == used and back[2].is_empty(),
        "a backup keeps an empty slot empty", back[2].name)

    sparse = os.path.join(work, "sparse.mfprojz")
    B.write_bank(sparse, [(5, mccfile.preset_archive("Five", 0, used, 0x33))],
                 top="sparse", subdir="01-sparse-A", ext="mbp")
    got = list(B.read_bank(sparse))
    chk([(i.slot, i.is_empty()) for i in got]
        == [(1, True), (2, True), (3, True), (4, True), (5, False)],
        "slots before the last preset get empty entries, for MCC",
        "%d members" % len(got))
    B.write_bank(sparse, [(5, mccfile.preset_archive("Five", 0, used, 0x33))],
                 top="sparse", subdir="01-sparse-A", ext="mbp", pad=False)
    chk([i.slot for i in B.read_bank(sparse)] == [5], "unless pad=False")


def main():
    print(__doc__.splitlines()[0])
    with tempfile.TemporaryDirectory() as work:
        test_audio(work)
        test_preset(work)
        test_banks(work)
    print("\n%s" % ("FAIL - %d check(s) failed" % len(fails) if fails
                    else "PASS - MCC files read, write and convert"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
