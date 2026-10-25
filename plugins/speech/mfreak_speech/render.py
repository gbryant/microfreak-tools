#!/usr/bin/env python3
"""A Python port of Plaits' LPCSpeechSynth, so vocabulary can be auditioned
before any firmware is touched.

Ported from `pichenettes/eurorack` plaits/dsp/speech/lpc_speech_synth.cc (MIT,
Emilie Gillet), which the MicroFreak's Speech engine *is* -- see
microfreak-emu/docs/SPEECH.md for the byte-exact evidence.

    excitation     the 640-entry int8 glottal pulse, taken from the MIT
                   original so that rendering needs no firmware image
    period == 0    unvoiced: the excitation is noise
    period != 0    voiced: frequency_ = 1/period, period in 8 kHz samples
                   (`base_f0 = 100.0f / 8000.0f` in the original pins the rate)
    energy/256     amplitude
    k0,k1 / 32768  reflection coefficients; k2..k9 / 128

The note's pitch, not the frame, sets F0 on the instrument: the controller
passes `pitch_shift` and the frame's `period` only supplies the prosody
contour. Rendering here at 8 kHz with `pitch_shift = 1.0` gives the recording's
own pitch, which is what you want when checking an analysis.
"""
# This file is a port of Plaits' LPCSpeechSynth and carries its excitation
# table, both from pichenettes/eurorack, MIT, Copyright 2016 Emilie Gillet.
# See NOTICE.
# SPDX-License-Identifier: MIT

import struct
import sys
import numpy as np

import microfreak.lpc as S

EXCITATION_ADDR = 0x0807C84C
EXCITATION_SIZE = 640
FIRMWARE_BASE = 0x0800C000

# Plaits' `lut_lpc_excitation_pulse` (plaits/resources.cc), MIT, Copyright 2016
# Emilie Gillet -- see NOTICE. Held here rather than read out of the firmware
# because the firmware's copy *is* this table: all 640 entries are identical in
# fw 5.0.0.2084, checked against the published source 2026-10-14. Taking it from
# the MIT original is what lets `mfreak speech preview` run with no Arturia
# firmware present at all.
EXCITATION_PULSE = [
       0,    0,    0,    0,    1,    1,    1,    2,    3,    3,    4,    5,
       6,    7,    8,    9,   10,   12,   13,   15,   17,   19,   21,   23,
      25,   28,   30,   33,   36,   38,   41,   44,   48,   51,   54,   57,
      61,   64,   68,   71,   75,   78,   82,   85,   89,   92,   95,   99,
     102,  105,  108,  110,  113,  115,  118,  120,  121,  123,  124,  125,
     126,  127,  127,  127,  127,  126,  125,  124,  122,  121,  118,  116,
     113,  110,  107,  104,  100,   96,   92,   87,   83,   78,   73,   68,
      63,   58,   53,   47,   42,   37,   31,   26,   21,   16,   11,    6,
       1,   -3,   -8,  -12,  -16,  -20,  -23,  -26,  -29,  -32,  -35,  -37,
     -39,  -40,  -42,  -43,  -43,  -44,  -44,  -44,  -44,  -44,  -43,  -42,
     -41,  -40,  -39,  -38,  -36,  -35,  -33,  -32,  -30,  -29,  -27,  -26,
     -24,  -23,  -22,  -20,  -19,  -18,  -18,  -17,  -16,  -16,  -16,  -16,
     -16,  -16,  -17,  -17,  -18,  -19,  -20,  -21,  -23,  -24,  -26,  -27,
     -29,  -30,  -32,  -34,  -35,  -37,  -39,  -40,  -42,  -43,  -44,  -46,
     -47,  -48,  -48,  -49,  -49,  -50,  -50,  -50,  -49,  -49,  -48,  -47,
     -46,  -45,  -44,  -42,  -41,  -39,  -37,  -35,  -33,  -31,  -29,  -27,
     -25,  -23,  -20,  -18,  -16,  -14,  -13,  -11,   -9,   -8,   -7,   -6,
      -5,   -4,   -3,   -3,   -3,   -3,   -3,   -4,   -5,   -6,   -7,   -9,
     -10,  -12,  -14,  -16,  -18,  -21,  -23,  -26,  -29,  -32,  -34,  -37,
     -40,  -43,  -46,  -49,  -51,  -54,  -56,  -59,  -61,  -63,  -65,  -67,
     -68,  -70,  -71,  -72,  -72,  -73,  -73,  -73,  -73,  -72,  -72,  -71,
     -70,  -69,  -67,  -66,  -64,  -63,  -61,  -59,  -57,  -54,  -52,  -50,
     -48,  -45,  -43,  -41,  -39,  -37,  -35,  -33,  -31,  -29,  -27,  -26,
     -25,  -23,  -22,  -21,  -20,  -20,  -19,  -19,  -19,  -18,  -18,  -18,
     -19,  -19,  -19,  -20,  -20,  -21,  -21,  -22,  -22,  -23,  -23,  -24,
     -24,  -25,  -25,  -25,  -26,  -26,  -26,  -25,  -25,  -25,  -24,  -24,
     -23,  -22,  -21,  -20,  -19,  -18,  -16,  -15,  -13,  -12,  -10,   -8,
      -6,   -4,   -3,   -1,    1,    3,    5,    7,    8,   10,   12,   13,
      15,   16,   17,   18,   19,   20,   21,   21,   22,   22,   22,   22,
      22,   22,   22,   22,   21,   21,   20,   19,   19,   18,   17,   16,
      15,   14,   14,   13,   12,   11,   10,   10,    9,    9,    8,    8,
       8,    8,    8,    8,    8,    8,    8,    9,    9,   10,   11,   12,
      13,   14,   15,   16,   17,   18,   19,   21,   22,   23,   24,   26,
      27,   28,   29,   30,   31,   32,   33,   33,   34,   35,   35,   35,
      36,   36,   36,   36,   35,   35,   35,   34,   34,   33,   33,   32,
      31,   30,   30,   29,   28,   27,   26,   25,   25,   24,   23,   22,
      22,   21,   20,   20,   19,   19,   19,   19,   19,   19,   19,   19,
      19,   19,   20,   20,   21,   21,   22,   22,   23,   23,   24,   25,
      25,   26,   27,   27,   28,   29,   29,   29,   30,   30,   30,   31,
      31,   31,   31,   30,   30,   30,   29,   29,   28,   28,   27,   26,
      26,   25,   24,   23,   22,   21,   20,   19,   18,   17,   16,   15,
      15,   14,   13,   12,   12,   11,   10,   10,   10,    9,    9,    9,
       9,    9,    9,    9,    9,   10,   10,   10,   11,   11,   12,   12,
      12,   13,   13,   14,   14,   14,   15,   15,   15,   15,   15,   15,
      15,   15,   15,   15,   14,   14,   14,   13,   12,   12,   11,   10,
       9,    8,    8,    7,    6,    5,    4,    3,    2,    1,    0,   -1,
      -2,   -3,   -4,   -4,   -5,   -6,   -6,   -7,   -7,   -8,   -8,   -8,
      -9,   -9,   -9,   -9,   -9,   -9,   -9,   -9,   -8,   -8,   -8,   -7,
      -7,   -7,   -6,   -6,   -5,   -5,   -5,   -4,   -4,   -4,   -3,   -3,
      -3,   -2,   -2,   -2,   -2,   -1,   -1,   -1,   -1,   -1,   -1,   -1,
      -1,   -1,   -1,   -1,   -1,   -1,   -2,   -2,   -2,   -2,   -2,   -2,
      -2,   -2,   -2,   -2,   -2,   -2,   -2,   -2,   -2,   -2,   -2,   -2,
      -2,   -2,   -2,   -2,   -2,   -1,   -1,   -1,   -1,   -1,   -1,    0,
       0,    0,    0,    0,
]


def excitation_lut(main_raw=None):
    """The glottal pulse. Plaits' MIT table, or the same table from an image.

    Called with nothing this returns the MIT data, so rendering and auditioning
    work on a machine that has no firmware. Called with an image it returns what
    that image actually holds -- the ground truth for the firmware in hand --
    and warns if the two disagree, which would mean a revision changed the
    pulse. They agree in 5.0.0.2084.
    """
    if main_raw is None:
        return np.array(EXCITATION_PULSE, dtype=np.float32)
    d = main_raw if isinstance(main_raw, (bytes, bytearray)) else open(main_raw, "rb").read()
    off = EXCITATION_ADDR - FIRMWARE_BASE
    got = np.frombuffer(d[off:off + EXCITATION_SIZE], dtype=np.int8).astype(np.float32)
    if len(got) == EXCITATION_SIZE and not np.array_equal(
            got, np.array(EXCITATION_PULSE, dtype=np.float32)):
        n = int((got != np.array(EXCITATION_PULSE, dtype=np.float32)).sum())
        print("warning: this image's excitation pulse differs from Plaits' in "
              "%d of %d entries" % (n, EXCITATION_SIZE), file=sys.stderr)
    return got


def _this_blep(t):
    return 0.5 * t * t


def _next_blep(t):
    return -0.5 * (1.0 - t) * (1.0 - t)


def synth(frames, lut, sample_rate=8000, pitch_shift=1.0, prosody_amount=1.0,
          fps=S.FPS, seed=1):
    """-> float32 mono, the lattice run over `frames` with interpolation.

    One frame pair per 1/fps second, blended sample by sample, exactly as
    LPCSpeechSynthController does between playback_frame_ and the next."""
    rng = np.random.default_rng(seed)
    spf = max(1, int(round(sample_rate / fps)))
    base_f0 = 100.0 / 8000.0
    k = np.zeros(10, dtype=np.float64)
    s = np.zeros(11, dtype=np.float64)
    phase = 0.0
    pulse_idx = EXCITATION_SIZE          # start past the end: no pulse pending
    next_sample = 0.0
    out = []

    def coeffs(f):
        return np.array([f["k0"] / 32768.0, f["k1"] / 32768.0] +
                        [f["k%d" % i] / 128.0 for i in range(2, 10)])

    for fi in range(len(frames) - 1):
        f1, f2 = frames[fi], frames[fi + 1]
        k1v, k2v = coeffs(f1), coeffs(f2)
        e1, e2 = f1["energy"] / 256.0, f2["energy"] / 256.0
        p1 = 1.0 / f1["period"] if f1["period"] else base_f0
        p2 = 1.0 / f2["period"] if f2["period"] else base_f0
        n1 = e1 if f1["period"] == 0 else 0.0
        n2 = e2 if f2["period"] == 0 else 0.0
        u1 = e1 if f1["period"] != 0 else 0.0
        u2 = e2 if f2["period"] != 0 else 0.0
        for j in range(spf):
            b = j / spf
            k[:] = k1v + (k2v - k1v) * b
            noise_energy = n1 + (n2 - n1) * b
            pulse_energy = u1 + (u2 - u1) * b
            freq = p1 + (p2 - p1) * b
            f = (base_f0 + (freq - base_f0) * prosody_amount) * pitch_shift
            f = min(max(f, 0.0), 0.5)

            phase += f
            this_sample = next_sample
            next_sample = 0.0
            if phase >= 1.0:
                phase -= 1.0
                reset_time = phase / f if f > 0 else 0.0
                reset_sample = int(32.0 * reset_time)
                disc = 0.0
                if pulse_idx < EXCITATION_SIZE:
                    pulse_idx -= reset_sample
                    if 0 <= pulse_idx < EXCITATION_SIZE:
                        disc = lut[pulse_idx] / 128.0 * pulse_energy
                this_sample += -disc * _this_blep(reset_time)
                next_sample += -disc * _next_blep(reset_time)
                pulse_idx = reset_sample

            e = np.zeros(11)
            e[10] = noise_energy if rng.random() > 0.5 else -noise_energy
            if pulse_idx < EXCITATION_SIZE:
                next_sample += lut[pulse_idx] / 128.0 * pulse_energy
                pulse_idx += 32
            e[10] += this_sample
            e[10] *= 1.5
            for i in range(9, -1, -1):
                e[i] = e[i + 1] - k[i] * s[i]
            e[0] = min(max(e[0], -2.0), 2.0)
            for i in range(9, 0, -1):
                s[i] = s[i - 1] + k[i - 1] * e[i - 1]
            s[0] = e[0]
            out.append(e[0])
    return np.asarray(out, dtype=np.float32)


def write_wav(path, x, sample_rate=8000, gain=0.9):
    import wave
    peak = float(np.abs(x).max()) or 1.0
    pcm = np.clip(x / peak * gain * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
