#!/usr/bin/env python3
"""Convert speech audio into LPC frames for the Speech engine.

    mfreak speech analyze hello.wav there.wav --out vocab.json
    mfreak speech analyze --selftest --image stock.mff

The frames are quantised exactly as the firmware stores them, so this is
what the instrument will play. Use `mfreak speech preview` to hear it first.
"""

# The engine runs 10th-order LPC at 8 kHz in 25 ms frames: lpc_speech_synth.cc
# normalises pitch against 100/8000 and advances at kLPCSpeechSynthFPS = 40,
# so 200 samples per frame. Frames are quantised by speech_lpc into the
# firmware's own LUTs.
#
# The reflection-coefficient sign differs between textbooks; the lattice in
# LPCSpeechSynth::Render decides it. --selftest resynthesises a factory word,
# re-analyses it and correlates under both signs. K_SIGN is the winner, and
# the test fails if the margin is not decisive.
#
# Voicing is an autocorrelation-peak decision. period == 0 means unvoiced
# (noise excitation). Wrong voicing is far more audible than a slightly wrong
# period, and the played note sets F0 anyway.


import argparse, errno, json, os, subprocess, sys, tempfile, wave
import numpy as np

import microfreak.lpc as S
from microfreak import jsonout

RATE = 8000
HOP = RATE // int(S.FPS)          # 200
WINDOW = 300
ORDER = 10
PREEMPH = 0.9375
K_SIGN = +1.0                     # set by --selftest, see the note above
VOICED_THRESHOLD = 0.30
# Of full scale, below which a frame is emitted as silence.
#
# LOWERED from 0.012 on 2026-10-05.  At 0.012 this discarded every fricative:
# the /s/ onset of "speaks" measures rms 0.0009..0.0033 and the /f/ of "freak"
# 0.0009..0.0043, so 22 of 33 and 13 of 26 frames respectively came out as
# energy 0 -- taking the onsets and releases with them.  The owner heard that as
# "the beginning and ends might not have had enough enunciation".
#
# True silence in the same recordings is 0.00005..0.0002, two orders of
# magnitude below the consonants, so there is a wide gap to put the floor in.
# Factory bank_4 word 7 starts at energy 3, 3, 4, 10 -- Arturia keeps quiet
# onsets, and the linear rms -> energy map already reproduces that range
# (rms 0.003 -> energy 3).
SILENCE_FLOOR = 0.0005
MIN_LAG, MAX_LAG = 16, 160        # period_lut_ spans exactly this, 50..500 Hz


def load_audio(path, rate=RATE):
    """-> float32 mono at `rate`, via ffmpeg so any input format works.

    A missing path or a directory raises the OSError the CLI turns into a
    one-line refusal, and a file ffmpeg cannot read exits with one line.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
    if os.path.isdir(path):
        raise IsADirectoryError(errno.EISDIR, "a directory, not an audio file", path)
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "a.wav")
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", path,
               "-ac", "1", "-ar", str(rate), "-c:a", "pcm_s16le", out]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            sys.exit("%s: not an audio file ffmpeg can read (%s)"
                     % (path, (p.stderr.strip().splitlines() or ["no detail"])[-1]))
        with wave.open(out, "rb") as w:
            raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return x


def levinson(r, order, sign=K_SIGN):
    """-> reflection coefficients k[0..order-1] from autocorrelation r."""
    a = np.zeros(order + 1)
    a[0] = 1.0
    e = r[0]
    ks = np.zeros(order)
    if e <= 0:
        return ks
    for i in range(1, order + 1):
        acc = r[i] + np.dot(a[1:i], r[i - 1:0:-1]) if i > 1 else r[i]
        k = -acc / e
        k = float(np.clip(k, -0.999, 0.999))
        ks[i - 1] = sign * k
        new = a.copy()
        for j in range(1, i):
            new[j] = a[j] + k * a[i - j]
        new[i] = k
        a = new
        e *= (1.0 - k * k)
        if e <= 1e-12:
            break
    return ks


def analyse(x, sign=K_SIGN, quantise=True):
    """-> list of frame dicts.

    `quantise` round-trips the frames through the bitstream LUTs so they are
    exactly what the device would reconstruct.  That is **required** for the
    flash path, where the bitstream is what ships -- but it is pure loss for the
    SysEx path, which writes decoded `Frame` structs and can carry any energy
    0..255 and exact coefficients.

    The cost, measured 2026-10-05: energy dynamics. Raw
    `86,191,199,191,187,168,157,141,130,102,88,69,47,31` becomes
    `81,161,161,161,161,161,161,161,114,114,81,57,41,32` -- pinned flat, because
    `ENERGY` jumps 114 -> 161 -> 255.

    It does **not** cause the dropped word onsets; the zero counts are identical
    either way. That was `SILENCE_FLOOR`, and I blamed the LUT first.
    """
    pre = np.append(x[0], x[1:] - PREEMPH * x[:-1])
    win = np.hamming(WINDOW)
    frames = []
    n = len(x)
    for start in range(0, max(1, n - WINDOW + 1), HOP):
        seg_raw = x[start:start + WINDOW]
        seg = pre[start:start + WINDOW]
        if len(seg) < WINDOW:
            break
        rms = float(np.sqrt((seg_raw ** 2).mean()))
        if rms < SILENCE_FLOOR:
            frames.append(dict.fromkeys(S.FRAME_FIELDS, 0))
            continue
        w = seg * win
        ac = np.correlate(w, w, mode="full")[WINDOW - 1:]
        if ac[0] <= 0:
            frames.append(dict.fromkeys(S.FRAME_FIELDS, 0))
            continue
        ks = levinson(ac, ORDER, sign)

        # voicing and period from the raw (un-pre-emphasised) autocorrelation
        wr = seg_raw * win
        acr = np.correlate(wr, wr, mode="full")[WINDOW - 1:]
        lag_hi = min(MAX_LAG, len(acr) - 1)
        if lag_hi > MIN_LAG and acr[0] > 0:
            seg_ac = acr[MIN_LAG:lag_hi + 1]
            lag = int(np.argmax(seg_ac)) + MIN_LAG
            strength = float(acr[lag] / acr[0])
        else:
            lag, strength = 0, 0.0
        voiced = strength >= VOICED_THRESHOLD
        period = lag if voiced else 0

        energy = int(round(min(1.0, rms / 0.25) * 255))
        f = {"energy": max(1, energy), "period": period}
        f["k0"] = int(round(ks[0] * 32768))
        f["k1"] = int(round(ks[1] * 32768))
        for i in range(2, ORDER):
            f["k%d" % i] = int(round(ks[i] * 128))
        for name, lo, hi in (("k0", -32768, 32767), ("k1", -32768, 32767)):
            f[name] = int(np.clip(f[name], lo, hi))
        for i in range(2, ORDER):
            f["k%d" % i] = int(np.clip(f["k%d" % i], -128, 127))
        frames.append(f)
    # Quantise through the real LUTs by a decode(encode()) round trip, so the
    # frames held here are exactly the ones the device will reconstruct.
    if frames and quantise:
        blob = S.encode_word(frames)
        frames, _ = S.decode_word(blob)
    return frames


def _selftest(image):
    import mfreak_speech.render as R
    from microfreak.firmware.image import main_code
    d = bytes(main_code(image))
    lut = R.excitation_lut(d)
    base = 0x0800C000
    blob = d[0x08078D34 - base:0x08078D34 - base + 900]      # bank_1, digits
    fr, bd = S.decode_bank(blob)
    words = S.split_words(fr, bd)
    print("sign test: resynthesise a factory word, re-analyse, compare k0/k1")
    print("%-6s %-10s %-10s" % ("sign", "corr k0", "corr k1"))
    results = {}
    for sign in (+1.0, -1.0):
        c0, c1 = [], []
        for wi in (2, 5, 8):
            w = words[wi]
            x = R.synth(w, lut)
            got = analyse(x, sign=sign)
            m = min(len(w), len(got))
            if m < 4:
                continue
            a0 = np.array([f["k0"] for f in w[:m]], dtype=float)
            b0 = np.array([f["k0"] for f in got[:m]], dtype=float)
            a1 = np.array([f["k1"] for f in w[:m]], dtype=float)
            b1 = np.array([f["k1"] for f in got[:m]], dtype=float)
            if a0.std() > 0 and b0.std() > 0:
                c0.append(float(np.corrcoef(a0, b0)[0, 1]))
            if a1.std() > 0 and b1.std() > 0:
                c1.append(float(np.corrcoef(a1, b1)[0, 1]))
        results[sign] = (np.mean(c0) if c0 else 0.0, np.mean(c1) if c1 else 0.0)
        print("%-6.0f %-10.3f %-10.3f" % (sign, results[sign][0], results[sign][1]))
    best = max(results, key=lambda s: results[s][0] + results[s][1])
    margin = abs((results[+1.0][0] + results[+1.0][1]) -
                 (results[-1.0][0] + results[-1.0][1]))
    print()
    print("winning sign: %+.0f   margin %.3f   K_SIGN in this file is %+.0f"
          % (best, margin, K_SIGN))
    if margin < 0.2:
        print("MARGIN TOO SMALL -- the sign is not established by this test")
        return 1
    if best != K_SIGN:
        print("K_SIGN IS WRONG -- set K_SIGN = %+.1f" % best)
        return 1
    print("K_SIGN is correct")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("words", nargs="*", help="one audio file per word, in order")
    ap.add_argument("--out", help="write the frames to this JSON file")
    ap.add_argument("--selftest", action="store_true",
                    help="check the analysis against a factory word; needs "
                         "--image")
    ap.add_argument("--image", metavar="FILE",
                    help="the stock firmware, as downloaded")
    a = ap.parse_args()
    if a.selftest:
        if not a.image:
            ap.error("--selftest needs --image")
        sys.exit(_selftest(a.image))
    if not a.words:
        ap.error("give at least one audio file, or --selftest")
    out = []
    for p in a.words:
        fr = analyse(load_audio(p))
        out.append({"source": p, "frames": fr})
        print("%-40s %3d frames  %.2f s" % (os.path.basename(p), len(fr), len(fr) / S.FPS))
    jsonout.result(
        out=a.out,
        words=[{"source": o["source"], "frames": len(o["frames"]),
                "seconds": round(len(o["frames"]) / S.FPS, 3)} for o in out])
    if a.out:
        json.dump(out, open(a.out, "w"))
        print("wrote %s" % a.out)


if __name__ == "__main__":
    main()
