#!/usr/bin/env python3
"""Convert a WAV file into an MCC wavetable file (.mfwz).

    mfreak wavetable encode pad.wav pad.mfwz
    mfreak wavetable encode serum.wav serum.mfwz --cycle-samples 2048
    mfreak wavetable write 3 --file pad.mfwz --yes

A wavetable is 32 cycles of 256 samples. With --cycle-samples, the input is
treated as cycles of that length: the first and last are kept and the rest
are spaced evenly between them, as MIDI Control Center does. Without it, the
file is cut into 32 equal pieces.

The input's sample rate does not matter. The name is the input's file name
unless --name gives another.
"""
import argparse
import os
import sys

import numpy as np

from microfreak.sample.encode import BLOCK_PAD, BLOCK_SAMPLES, decode_7to8, load_wav_mono, pack_7to8
from microfreak import jsonout

FRAMES = 4
CYCLES = 32
CYCLE_SAMPLES = 256
TOTAL_SAMPLES = FRAMES * BLOCK_SAMPLES  # 8192, == CYCLES * CYCLE_SAMPLES


def resample_to_length(samples, n_out):
    """Fits `samples` to exactly n_out points by fractional position, not by
    rate -- there's no real playback rate for wavetable content to preserve.
    """
    # A zero-length input reached np.interp and came out as
    # "ValueError: array of sample points is empty" -- a numpy traceback for
    # what is really a bad file. The len == 1 case was already special-cased
    # just below, so this is the same guard one step earlier.
    if len(samples) == 0:
        raise ValueError(
            "the audio is empty. A wavetable needs at least one cycle.")
    if len(samples) == 1:
        return np.full(n_out, samples[0], dtype=np.float64)
    src_t = np.linspace(0, 1, len(samples), endpoint=False)
    dst_t = np.linspace(0, 1, n_out, endpoint=False)
    return np.interp(dst_t, src_t, samples)


def map_by_slicing(samples):
    """No cycle info given: slice the whole file's time axis into 32 equal
    segments, each independently resampled to 256 samples.
    """
    n = len(samples)
    edges = np.linspace(0, n, CYCLES + 1)
    out = np.empty((CYCLES, CYCLE_SAMPLES))
    for j in range(CYCLES):
        lo, hi = int(edges[j]), int(edges[j + 1])
        seg = samples[lo:hi] if hi > lo else samples[max(0, lo - 1):lo + 1]
        out[j] = resample_to_length(seg, CYCLE_SAMPLES)
    return out.reshape(-1)


def map_by_cycles(samples, cycle_samples):
    """Cycle-aware mapping matching MCC's documented import behavior: source
    cycle 0 -> output cycle 0, source's last cycle -> output cycle 31,
    evenly-spaced positions in between crossfaded between their two nearest
    source cycles. Works uniformly whether the source has more or fewer
    than 32 cycles.
    """
    n_cycles = len(samples) // cycle_samples
    if n_cycles < 1:
        raise ValueError(f"input is shorter than one {cycle_samples}-sample cycle")
    trimmed = samples[:n_cycles * cycle_samples]
    cycles = trimmed.reshape(n_cycles, cycle_samples)
    cycles_256 = np.array([resample_to_length(c, CYCLE_SAMPLES) for c in cycles])

    if n_cycles == 1:
        return np.tile(cycles_256[0], CYCLES)

    out = np.empty((CYCLES, CYCLE_SAMPLES))
    for j in range(CYCLES):
        pos = j * (n_cycles - 1) / (CYCLES - 1)
        i0 = int(np.floor(pos))
        i1 = min(i0 + 1, n_cycles - 1)
        frac = pos - i0
        out[j] = cycles_256[i0] * (1 - frac) + cycles_256[i1] * frac
    return out.reshape(-1)


def encode_block(block_samples):
    """block_samples: exactly BLOCK_SAMPLES (2048) int16 values. Returns the
    4704-byte wire-packed frame."""
    assert len(block_samples) == BLOCK_SAMPLES
    b32 = block_samples.astype(np.int32)
    lo = (b32 & 0xFF).astype(np.int16)
    hi = (b32 >> 8).astype(np.int16)
    lo = np.where(lo >= 128, lo - 256, lo)
    real = np.empty(BLOCK_SAMPLES * 2, dtype=np.int16)
    real[0::2] = lo
    real[1::2] = hi
    real_bytes = list(real) + [0] * BLOCK_PAD
    return pack_7to8(real_bytes)


def encode(path, cycle_samples=None):
    samples, _rate = load_wav_mono(path)
    if cycle_samples:
        mapped = map_by_cycles(samples, cycle_samples)
    else:
        mapped = map_by_slicing(samples)
    s16 = np.clip(np.round(mapped), -32768, 32767).astype(np.int16)

    frames = []
    for f in range(FRAMES):
        block = s16[f * BLOCK_SAMPLES:(f + 1) * BLOCK_SAMPLES]
        frames.append(encode_block(block))
    return frames, s16


def self_check(frames, expected_s16):
    got = []
    for frame_wire in frames:
        real = decode_7to8(list(frame_wire))[:BLOCK_SAMPLES * 2]
        for i in range(0, len(real), 2):
            lo = real[i] & 0xFF
            hi = real[i + 1]
            val = lo | ((hi & 0xFF) << 8)
            got.append(val - 65536 if val >= 32768 else val)
    got = np.array(got, dtype=np.int16)
    return np.array_equal(got, expected_s16), len(expected_s16), len(got)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_wav", help="source audio")
    ap.add_argument("output", metavar="OUTPUT.mfwz",
                    help="the MCC wavetable file to write")
    ap.add_argument("--name", help="the wavetable's name, up to 16 characters "
                    "(default: the input's file name)")
    ap.add_argument("--cycle-samples", type=int,
                     help="the input's cycle length, e.g. 2048 for a Serum "
                          "export. Omit for ordinary audio")
    args = ap.parse_args()
    from microfreak import mccfile
    name = args.name
    if name is None:
        name = os.path.splitext(os.path.basename(args.input_wav))[0][
            :mccfile.NAME_LIMIT["wavetable"]]
    try:
        mccfile.check_ext(args.output, "wavetable")
        mccfile.check_name(name, "wavetable")
    except mccfile.FileError as exc:
        ap.error(str(exc))

    # An unusable input is a refusal, not a traceback. cli._invoke translates
    # only file errors, on purpose -- "anything else IS a bug" -- and an empty
    # or too-short WAV is neither a file error nor a bug.
    try:
        frames, expected = encode(args.input_wav, args.cycle_samples)
    except ValueError as exc:
        sys.exit("%s: %s" % (args.input_wav, exc))
    ok, n_expected, n_got = self_check(frames, expected)
    print(f"self-check: {'OK' if ok else 'MISMATCH'} ({n_got} decoded samples vs {n_expected} expected)")
    if not ok:
        sys.exit("encoder self-check failed -- not writing output")

    pcm = mccfile.pcm_from_s16(expected)
    if mccfile.wavetable_frames(pcm) != frames:
        sys.exit("encoder self-check failed -- not writing output")
    mccfile.write_one(args.output, mccfile.audio_archive(name, pcm, "wavetable"),
                      "wavetable")
    print(f"wrote {args.output}: {name!r}")
    jsonout.result(input=args.input_wav, out=args.output, name=name,
                   self_check="ok", decoded_samples=n_got)


if __name__ == "__main__":
    main()
