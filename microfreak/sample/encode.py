#!/usr/bin/env python3
"""Convert a WAV file into an MCC sample file (.mfsz) for `mfreak sample write`.

CONFIRMED CORRECT. Cracked via a known-plaintext attack using MCC's own
`.mfsz` export (a ZIP containing a readable Boost.Serialization text array
of the sample's internal PCM) compared byte-for-byte against the real wire
bytes MCC sent for the same sample -- 100% exact match. See
docs/SAMPLE_ENCODING.md for the full investigation.

The encoding:
    1. 16-bit signed PCM, little-endian, 32kHz, mono.
    2. Grouped into 4096-byte blocks (2048 samples) + 20 bytes of zero
       padding = 4116 "real" bytes per block (the fixed size of one
       147-chunk / 4704-byte wire transport block, after unpacking).
    3. Each 4116-byte block is packed with the standard MIDI SysEx 7-to-8
       scheme: every 7 real (signed 8-bit) bytes become 8 wire bytes --
       one header byte (bit i = bit 7 of the (i+1)-th real byte in the
       group) followed by the 7 real bytes with their bit 7 stripped
       (i.e. masked to 0-127). 4116 / 7 = 588 groups exactly, x8 = 4704
       wire bytes per block, matching `sample.write`'s BLOCK_CHUNKS=147
       (147 x 32 = 4704) exactly.

Verified round-trip: encoding then decoding reproduces the original 16-bit
samples exactly (lossless, aside from the resample/quantization needed to
get from the source WAV's own rate/depth to 32kHz/16-bit in the first
place).

usage: mfreak sample encode INPUT.wav OUTPUT.mfsz
"""
import argparse
import os
import sys
import wave

import numpy as np
from microfreak import jsonout

TARGET_RATE = 32000
BLOCK_SAMPLES = 2048   # 4096 bytes of 16-bit LE audio per block
BLOCK_PAD = 20         # trailing zero real-bytes per block (confirmed structural)
REAL_BYTES_PER_BLOCK = BLOCK_SAMPLES * 2 + BLOCK_PAD  # 4116


def load_wav_mono(path):
    with wave.open(path, "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        n = w.getnframes()
        raw = w.readframes(n)

    if width != 2:
        raise ValueError(f"only 16-bit WAV supported, got {width*8}-bit")

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate


def resample_linear(samples, src_rate, dst_rate):
    duration = len(samples) / src_rate
    n_out = int(round(duration * dst_rate))
    src_t = np.arange(len(samples)) / src_rate
    dst_t = np.arange(n_out) / dst_rate
    return np.interp(dst_t, src_t, samples)


# encode() and self_check() must truncate identically; one constant so they
# cannot drift apart again.
MAX_SECONDS = 24.0


def pack_7to8(real_bytes):
    """real_bytes: iterable of ints in -128..127 (or 0..255), length a
    multiple of 7. Returns the packed wire bytes (length * 8/7)."""
    real_bytes = bytes(b & 0xFF for b in real_bytes)
    assert len(real_bytes) % 7 == 0
    out = bytearray()
    for g in range(0, len(real_bytes), 7):
        group = real_bytes[g:g + 7]
        header = 0
        for i, b in enumerate(group):
            header |= ((b >> 7) & 1) << i
        out.append(header)
        out.extend(b & 0x7F for b in group)
    return bytes(out)


def decode_7to8(wire_bytes):
    """Inverse of pack_7to8, returned as signed ints -128..127. Used here
    only for the round-trip self-check.
    """
    out = []
    for g in range(0, len(wire_bytes) - 7, 8):
        header = wire_bytes[g]
        for i in range(7):
            b = wire_bytes[g + 1 + i]
            bit7 = (header >> i) & 1
            val = b | (bit7 << 7)
            out.append(val - 256 if val >= 128 else val)
    return out


def encode(path, target_rate=TARGET_RATE, max_seconds=MAX_SECONDS):
    """-> (16-bit LE PCM at 32 kHz, seconds) for a WAV, cut to max_seconds."""
    samples, rate = load_wav_mono(path)
    samples = resample_linear(samples, rate, target_rate)

    max_samples = int(max_seconds * target_rate)
    if len(samples) > max_samples:
        samples = samples[:max_samples]

    s16 = np.clip(np.round(samples), -32768, 32767).astype(np.int16)
    return s16.astype("<i2").tobytes(), len(samples) / target_rate


def self_check(wav_path, wire_bytes, max_seconds=MAX_SECONDS):
    """Decodes wire_bytes back and compares against the original resampled
    16-bit samples, to catch encoder bugs before anything gets sent live.

    It must apply the SAME truncation `encode` does, off the same constant.
    It did not, so every source longer than MAX_SECONDS compared the 24 s
    that were encoded against the full-length original and reported a
    mismatch -- a correct encode that could never be written. Measured:
    23 s and 24 s passed, 26 s and 40 s always failed.
    """
    samples, rate = load_wav_mono(wav_path)
    samples = resample_linear(samples, rate, TARGET_RATE)
    max_samples = int(max_seconds * TARGET_RATE)
    if len(samples) > max_samples:
        samples = samples[:max_samples]
    expected = np.clip(np.round(samples), -32768, 32767).astype(np.int16)

    got = []
    for b in range(len(wire_bytes) // 4704):
        block_wire = wire_bytes[b * 4704:(b + 1) * 4704]
        real = decode_7to8(list(block_wire))[:BLOCK_SAMPLES * 2]
        for i in range(0, len(real), 2):
            lo = real[i] & 0xFF
            hi = real[i + 1]
            val = lo | ((hi & 0xFF) << 8)
            got.append(val - 65536 if val >= 32768 else val)
    got = np.array(got[:len(expected)], dtype=np.int16)
    match = np.array_equal(got, expected)
    return match, len(expected), len(got)


def main():
    from microfreak import mccfile
    ap = argparse.ArgumentParser(
        prog="mfreak sample encode",
        description=__doc__.splitlines()[0],
        epilog="    mfreak sample encode kick.wav kick.mfsz\n"
               "    mfreak sample write 60 --file kick.mfsz --yes\n\n"
               "The audio is converted to 32 kHz mono and cut at %d s. "
               "Offline. The output is checked by decoding it again."
               % MAX_SECONDS,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", metavar="INPUT.wav", help="source audio")
    ap.add_argument("output", metavar="OUTPUT.mfsz",
                    help="the MCC sample file to write")
    ap.add_argument("--name", help="the sample's name, up to %d characters "
                    "(default: the input's file name)"
                    % mccfile.NAME_LIMIT["sample"])
    a = ap.parse_args()
    inp, outp = a.input, a.output
    name = a.name
    if name is None:
        name = os.path.splitext(os.path.basename(inp))[0][:mccfile.NAME_LIMIT["sample"]]
    try:
        mccfile.check_ext(outp, "sample")
        mccfile.check_name(name, "sample")
    except mccfile.FileError as exc:
        ap.error(str(exc))
    pcm, duration = encode(inp)
    # Refuse an empty result instead of writing it: nothing could ever be
    # sent from it.
    if not pcm:
        sys.exit("%s is too short (%.3f s). A sample must be at least "
                 "%.2f s." % (inp, duration, BLOCK_SAMPLES / TARGET_RATE))

    wire = mccfile.sample_wire(pcm)
    ok, n_expected, n_got = self_check(inp, wire)
    status = "OK" if ok else "MISMATCH"
    print(f"self-check: {status} ({n_got} decoded samples vs {n_expected} expected)")
    if not ok:
        sys.exit("encoder self-check failed -- not writing output")

    mccfile.write_one(outp, mccfile.audio_archive(name, pcm, "sample"), "sample")
    print(f"encoded {duration:.2f}s -> {outp}: {name!r}")
    jsonout.result(input=inp, out=outp, name=name, seconds=round(duration, 3),
                   bytes=len(pcm), self_check="ok", decoded_samples=n_got)


if __name__ == "__main__":
    main()
