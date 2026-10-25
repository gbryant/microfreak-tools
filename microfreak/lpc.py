#!/usr/bin/env python3
"""LPC speech frames and the Plaits word-bank bitstream, encode and decode.

The MicroFreak's Speech engine is Mutable Instruments' Plaits LPC speech synth,
lifted whole -- data included. Established 2026-10-04 by three byte-exact
matches against `pichenettes/eurorack` (MIT): the 14-byte `Frame` struct, the
15-entry `phonemes_` table, and word banks 0 and 1. See microfreak-emu/docs/SPEECH.md.

    firmware layout (fw 5.0.0.2084, load base 0x0800C000)
    0x08078764  phonemes_[15]         15 x 14 bytes
    0x08078838  word_banks_[5]        5 x {const uint8_t* data; size_t size}
    0x08078860  bank_0  1233 bytes     7 words,  229 frames
    0x08078D34  bank_1   900 bytes    11 words,  229 frames   <- the digits
    0x080790B8  bank_2  1552 bytes    26 words,  524 frames
    0x080796C8  bank_3  2524 bytes    26 words,  604 frames
    0x0807A0A4  bank_4  4802 bytes    22 words,  926 frames
    0x0807C84C  excitation pulse LUT  640 int8

**The bitstream.** Per frame, MSB-first fields drawn from a stream that takes
each byte's bits LSB-first:

    4 bits   energy index   0 -> silent frame; 0xF -> end of word, then flush
    1 bit    repeat         reuse the previous frame's reflection coefficients
    6 bits   period index
    then, when repeat == 0:
    5,5      k0, k1   (int16 LUTs)
    4,4      k2, k3   (int8 LUTs)
    then, when period != 0 (voiced):
    4,4,4    k4, k5, k6
    3,3,3    k7, k8, k9

A frame carries forward every field the stream does not set, which is why
`repeat` and the unvoiced short form work at all. A word ends on energy 0xF
followed by a flush to the next byte boundary, so words are byte-aligned and
a bank is the concatenation of its words.

Quantisation is nearest-value into the same LUTs the firmware holds, so a
round trip through `decode` is exact and `encode(decode(bank)) == bank` for all
five factory banks -- which is how this module is tested.
"""
# The quantisation tables below and the bitstream layout are derived from
# pichenettes/eurorack (Plaits), MIT, Copyright 2016 Emilie Gillet. The rest
# of this file is Apache-2.0 like the project. See NOTICE.
# SPDX-License-Identifier: Apache-2.0 AND MIT

import struct

ENERGY = [0x00, 0x02, 0x03, 0x04, 0x05, 0x07, 0x0a, 0x0f,
          0x14, 0x20, 0x29, 0x39, 0x51, 0x72, 0xa1, 0xff]
PERIOD = [0, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31,
          32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 45, 47, 49, 51, 53,
          54, 57, 59, 61, 63, 66, 69, 71, 73, 77, 79, 81, 85, 87, 92, 95, 99,
          102, 106, 110, 115, 119, 123, 128, 133, 138, 143, 149, 154, 160]
K0 = [-32064, -31872, -31808, -31680, -31552, -31424, -31232, -30848,
      -30592, -30336, -30016, -29696, -29376, -28928, -28480, -27968,
      -26368, -24256, -21632, -18368, -14528, -10048, -5184, 0,
      5184, 10048, 14528, 18368, 21632, 24256, 26368, 27968]
K1 = [-20992, -19328, -17536, -15552, -13440, -11200, -8768, -6272,
      -3712, -1088, 1536, 4160, 6720, 9216, 11584, 13824,
      15936, 17856, 19648, 21248, 22656, 24000, 25152, 26176,
      27072, 27840, 28544, 29120, 29632, 30080, 30464, 32384]
K2 = [-110, -97, -83, -70, -56, -43, -29, -16, -2, 11, 25, 38, 52, 65, 79, 92]
K3 = [-82, -68, -54, -40, -26, -12, 1, 15, 29, 43, 57, 71, 85, 99, 113, 126]
K4 = [-82, -70, -59, -47, -35, -24, -12, -1, 11, 23, 34, 46, 57, 69, 81, 92]
K5 = [-64, -53, -42, -31, -20, -9, 3, 14, 25, 36, 47, 58, 69, 80, 91, 102]
K6 = [-77, -65, -53, -41, -29, -17, -5, 7, 19, 31, 43, 55, 67, 79, 90, 102]
K7 = [-64, -40, -16, 7, 31, 55, 79, 102]
K8 = [-64, -44, -24, -4, 16, 37, 57, 77]
K9 = [-51, -33, -15, 4, 22, 32, 59, 77]
KLUT = [K0, K1, K2, K3, K4, K5, K6, K7, K8, K9]
KBITS = [5, 5, 4, 4, 4, 4, 4, 3, 3, 3]

FRAME_FIELDS = ("energy", "period", "k0", "k1", "k2", "k3",
                "k4", "k5", "k6", "k7", "k8", "k9")
FRAME_BYTES = 14
FPS = 40.0
MAX_FRAMES = 1024            # kLPCSpeechSynthMaxFrames

# kLPCSpeechSynthMaxWords is 32, but 32 is NOT safe on this firmware.
# `word_boundaries_` is bank+0x14..bank+0x94 -- the memset in WordBank::Init is
# 0x80 bytes, so exactly 32 uint32 entries -- and the boundary table needs
# num_words+1 of them: 0x0805E928 writes bounds[num_words] as the final frame
# count.  With 32 words that store lands on bank+0x94, which is the `frames_`
# pointer itself.  Measured 2026-10-05; the factory banks peak at 26 words so
# nothing ships near it.
MAX_WORDS = 31               # 32 entries, minus one for the end boundary


def _rev(b):
    b = ((b >> 4) | (b << 4)) & 0xFF
    b = (((b & 0xCC) >> 2) | ((b & 0x33) << 2)) & 0xFF
    b = (((b & 0xAA) >> 1) | ((b & 0x55) << 1)) & 0xFF
    return b


class _Reader:
    """Mirrors Plaits' BitStream exactly, including Flush and ptr."""

    def __init__(self, data):
        self.p = data
        self.i = 0
        self.available = 0
        self.bits = 0

    def get(self, n):
        shift = n
        if n > self.available:
            self.bits = (self.bits << self.available) & 0xFFFF
            shift -= self.available
            self.bits |= _rev(self.p[self.i])
            self.i += 1
            self.available += 8
        self.bits = (self.bits << shift) & 0xFFFF
        out = (self.bits >> 8) & 0xFF
        self.bits &= 0xFF
        self.available -= n
        return out

    def flush(self):
        while self.available:
            self.get(1)


class _Writer:
    """The inverse: bits MSB-first per field, packed LSB-first per byte."""

    def __init__(self):
        self.bits = []

    def put(self, value, n):
        for k in range(n - 1, -1, -1):
            self.bits.append((value >> k) & 1)

    def flush_to_byte(self):
        """Pad a word's final byte with **1s**, which is what Arturia does.

        THE ENCODER BUG, found 2026-10-06.  This padded with 0s, and our own
        decoder never noticed: it stops at the 0xF end-of-word nibble and skips
        to the next byte, so a re-encode round-tripped through our decoder
        perfectly while being wrong on the wire.  Zero padding reads as
        `energy = 0` -- a *silent frame* -- so a decoder that keeps reading
        consumes phantom frames and every later word boundary shifts.  That is
        what "a 28-word bank decoded in Python but six of seven Word positions
        were silent on the device" was.

        Evidence: with 1s, re-encoding the factory banks is byte-identical for
        bank_0..bank_3.  bank_4 differs only in `repeat` choices -- and it is
        byte-identical under a never-repeat rule, which banks 1-3 are not, so
        the five banks were simply not all authored with the same encoder
        settings.  Our repeat choice was never the problem.
        """
        while len(self.bits) % 8:
            self.bits.append(1)

    def bytes(self):
        self.flush_to_byte()
        out = bytearray(len(self.bits) // 8)
        for idx, bit in enumerate(self.bits):
            if bit:
                out[idx // 8] |= 1 << (idx % 8)
        return bytes(out)


def _nearest(lut, value):
    best, bi = None, 0
    for i, v in enumerate(lut):
        d = abs(v - value)
        if best is None or d < best:
            best, bi = d, i
    return bi


def decode_word(data):
    """-> (frames, bytes_consumed) for one word starting at data[0]."""
    r = _Reader(data)
    f = dict.fromkeys(FRAME_FIELDS, 0)
    frames = []
    while True:
        e = r.get(4)
        if e == 0:
            f = dict(f)
            f["energy"] = 0
        elif e == 0xF:
            r.flush()
            break
        else:
            f = dict(f)
            f["energy"] = ENERGY[e]
            repeat = r.get(1)
            f["period"] = PERIOD[r.get(6)]
            if not repeat:
                f["k0"] = K0[r.get(5)]
                f["k1"] = K1[r.get(5)]
                f["k2"] = K2[r.get(4)]
                f["k3"] = K3[r.get(4)]
                if f["period"]:
                    f["k4"] = K4[r.get(4)]
                    f["k5"] = K5[r.get(4)]
                    f["k6"] = K6[r.get(4)]
                    f["k7"] = K7[r.get(3)]
                    f["k8"] = K8[r.get(3)]
                    f["k9"] = K9[r.get(3)]
        frames.append(dict(f))
    return frames, r.i


# The dynamic range the device's OWN vocabulary uses.  Measured across all 22
# words of factory bank_4: non-zero energies run 2..5 at the quiet end and peak
# at exactly 161 every time, for a median consonant-to-vowel ratio of 53.7:1.
# 161 is the top non-255 entry of the ENERGY LUT, so factory content never goes
# above it.
#
# Our own analysis ran 1..242 (ratio up to 242:1), 4-4.5x more extreme than
# anything the instrument plays, which is why word onsets and releases read as
# under-enunciated against factory sounds.
DEVICE_PEAK = 161
DEVICE_FLOOR = 3


def match_device_range(frames, peak=DEVICE_PEAK, floor=DEVICE_FLOOR):
    """-> `frames` rescaled into the energy range the factory words occupy.

    Per word: scale so the loudest frame lands on `peak`, then lift every
    remaining non-zero frame to at least `floor`.  Frames that are genuinely
    silent (energy 0) stay silent -- this changes dynamics, not word boundaries.

    This makes the words less faithful to the source recording on purpose. The
    goal is to sound like the instrument's own speech, not like the 22 kHz TTS
    file the frames were analysed from.
    """
    nz = [f["energy"] for f in frames if f["energy"]]
    if not nz:
        return list(frames)
    gain = float(peak) / max(nz)
    out = []
    for f in frames:
        g = dict(f)
        if g["energy"]:
            g["energy"] = max(floor, min(255, int(round(g["energy"] * gain))))
        out.append(g)
    return out


def trim_leading_silence(frames):
    """-> `frames` without its leading zero-energy frames.

    A frame with energy 0 at the START of a word plays as nothing: the engine
    does not skip ahead to find the audible part.  Every factory word begins
    with a non-zero energy (bank_4 word 7 starts at 3), and `say` output
    reliably does not -- it opens with silence.  Measured 2026-10-05: words
    with leading zero-energy frames render at exactly 0.0 RMS, words without
    them play.  Both the flash and SysEx paths must do this.
    """
    out = list(frames)
    while out and out[0]["energy"] == 0:
        out.pop(0)
    return out


def end_with_silence(frames):
    """-> `frames` ending in one zero-energy frame.

    A held key keeps playing a word's last frame. Every factory word ends on
    energy 0, so it falls silent; a word ending on sound drones instead.
    """
    out = list(frames)
    if out and out[-1]["energy"]:
        f = dict(out[-1])
        f["energy"] = 0
        out.append(f)
    return out


def word_byte_offsets(data):
    """-> the byte offset of each word start, plus the end.

    `decode_bank` reports boundaries in frames; truncating a bank's declared
    size needs them in bytes, because the loader decodes words until the size
    runs out and a cut mid-word would leave a partial bitstream.
    """
    offs, off = [], 0
    while off < len(data):
        offs.append(off)
        _, used = decode_word(data[off:])
        if used <= 0:
            raise ValueError("bitstream made no progress at %d" % off)
        off += used
    offs.append(off)
    return offs


def decode_bank(data):
    """-> (frames, word_boundaries) exactly as LPCSpeechSynthWordBank::Load."""
    frames, bounds, off = [], [], 0
    while off < len(data):
        bounds.append(len(frames))
        got, used = decode_word(data[off:])
        if used <= 0:
            raise ValueError("bitstream made no progress at %d" % off)
        frames.extend(got)
        off += used
    bounds.append(len(frames))
    return frames, bounds


def encode_word(frames):
    """One word -> bytes. `frames` is a list of dicts over FRAME_FIELDS.

    `repeat` is emitted whenever a frame's reflection coefficients are
    unchanged from the previous frame, which is what makes a re-encode of the
    factory banks byte-identical rather than merely equivalent."""
    w = _Writer()
    prev = dict.fromkeys(FRAME_FIELDS, 0)
    for f in frames:
        if f["energy"] == 0:
            w.put(0, 4)
            prev = dict(prev)
            prev["energy"] = 0
            continue
        w.put(_nearest(ENERGY[1:15], f["energy"]) + 1, 4)
        voiced = f["period"] != 0
        same = all(f[k] == prev[k] for k in ("k0", "k1", "k2", "k3")) and (
            not voiced or all(f[k] == prev[k]
                              for k in ("k4", "k5", "k6", "k7", "k8", "k9")))
        w.put(1 if same else 0, 1)
        w.put(_nearest(PERIOD, f["period"]), 6)
        if not same:
            for n, name in enumerate(("k0", "k1", "k2", "k3")):
                w.put(_nearest(KLUT[n], f[name]), KBITS[n])
            if voiced:
                for n, name in zip(range(4, 10),
                                   ("k4", "k5", "k6", "k7", "k8", "k9")):
                    w.put(_nearest(KLUT[n], f[name]), KBITS[n])
        prev = dict(f)
    w.put(0xF, 4)
    return w.bytes()


def encode_bank(words):
    """A list of words (each a list of frame dicts) -> one bank blob."""
    return b"".join(encode_word(fr) for fr in words)


def split_words(frames, bounds):
    return [frames[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]


def frames_to_bytes(frames):
    """The decoded 14-byte Frame form, for writing a frame table directly."""
    out = bytearray()
    for f in frames:
        out += struct.pack("<BBhhbbbbbbbb", f["energy"] & 0xFF, f["period"] & 0xFF,
                           f["k0"], f["k1"], f["k2"], f["k3"], f["k4"],
                           f["k5"], f["k6"], f["k7"], f["k8"], f["k9"])
    return bytes(out)


def bytes_to_frames(blob):
    out = []
    for i in range(0, len(blob) - FRAME_BYTES + 1, FRAME_BYTES):
        v = struct.unpack_from("<BBhhbbbbbbbb", blob, i)
        out.append(dict(zip(FRAME_FIELDS, v)))
    return out
