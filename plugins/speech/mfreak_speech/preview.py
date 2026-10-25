#!/usr/bin/env python3
"""Hear words the way the Speech engine will say them.

    mfreak speech preview "hello there"     speak text with the system voice
    mfreak speech preview word.wav          use an audio file
    mfreak speech preview                   record 3 s from the mic
    mfreak speech preview --note 48 hello   hear it at a played pitch
    mfreak speech preview --from vocab.txt  every word in a vocabulary file

It plays the source, then the Speech engine's version. No instrument or
firmware is needed. The playback uses the same synthesis
as the instrument, so this is what it will sound like.

The frame report counts the frames the engine will drop as silent. Quiet
consonants like /s/ are the usual loss. If they vanish, record louder.

On the instrument the key you play sets the pitch. Use --note to hear that;
without it you hear your own pitch.
"""
import argparse, os, sys, tempfile, wave
import numpy as np

from microfreak import audio
import mfreak_speech.analyze as A
import mfreak_speech.cli as C
import microfreak.lpc as S
import mfreak_speech.render as R
from microfreak import jsonout


def record(seconds, out, device=None):
    """Record mono from `device`, by name or number, into `out`."""
    print("recording %g s. Speak now." % seconds, flush=True)
    try:
        x, rec = audio.record(seconds, device, channels=1)
    except audio.AudioError as exc:
        sys.exit("%s\n`mfreak render --list-devices` shows the inputs." % exc)
    audio.write_wav(out, x[:, 0], rec.rate)
    print("recorded %s from %s" % (out, rec.name))
    return out


def play_file(path):
    """Play a WAV directly, or anything else decoded through ffmpeg."""
    try:
        x, rate = audio.read_wav(path)
    except (wave.Error, ValueError, EOFError):
        rate = 44100
        x = A.load_audio(path, rate=rate)
    audio.play(x, rate)


def write_wav(path, x, rate):
    # x can be empty with frames NON-empty: at about 40 ms of input the
    # analyser yields one frame and the synthesiser returns nothing, which
    # slips past the "no frames" guard and reached np.max as a raw
    # "zero-size array to reduction operation maximum" ValueError.
    if len(x) == 0:
        sys.exit("the recording is too short. It needs at least 80 ms.")
    pk = float(np.max(np.abs(x))) or 1.0
    pcm = (np.clip(x / pk * 0.98, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def report(frames):
    """What the engine kept, and what it threw away."""
    n = len(frames)
    silent = [i for i, f in enumerate(frames) if f["energy"] == 0]
    unvoiced = [i for i, f in enumerate(frames) if f["period"] == 0]
    print("\n%d frames, %.2f s at %g fps" % (n, n / S.FPS, S.FPS))
    print("  voiced   %3d  (%.0f%%)" % (n - len(unvoiced),
                                        100.0 * (n - len(unvoiced)) / max(n, 1)))
    print("  unvoiced %3d  (%.0f%%)   noise excitation, not the glottal pulse"
          % (len(unvoiced), 100.0 * len(unvoiced) / max(n, 1)))
    print("  SILENT   %3d  (%.0f%%)   energy 0 -- these play as nothing"
          % (len(silent), 100.0 * len(silent) / max(n, 1)))
    if silent:
        runs, start = [], silent[0]
        for a, b in zip(silent, silent[1:] + [None]):
            if b != (a + 1 if a is not None else None):
                runs.append((start, a)); start = b
        inner = [(a, b) for a, b in runs if a > 0 and b < n - 1]
        print("       dropped runs: %s"
              % ", ".join("%d-%d" % (a, b) for a, b in runs[:8]))
        if inner:
            print("       %d of them %s inside the word: quiet consonants. "
                  "Raise their level\n       relative to the vowels."
                  % (len(inner), "is" if len(inner) == 1 else "are"))
    e = [f["energy"] for f in frames if f["energy"]]
    if e:
        print("  energy   min %d  median %d  max %d   (15 levels exist)"
              % (min(e), int(np.median(e)), max(e)))


def preview_file(a, work, lut, shift):
    """Render every word of a vocabulary file, and play them in order."""
    import mfreak_speech.vocab as vocab
    banks = vocab.parse(a.vocab)
    played, n = [], 0
    for b, items in enumerate(banks):
        for label, path in C.resolve(items, a.voice, work):
            fr = C.word_frames(path, label)
            y = R.synth(fr, lut, pitch_shift=shift)
            out = os.path.join(work, "bank%d_%02d.wav" % (b + 1, n))
            write_wav(out, y, 8000)
            n += 1
            print("  bank %d  %-28s %4.1f s  %s" % (b + 1, label[:28], len(y) / 8000.0, out))
            played.append((b + 1, label, out))
    again = vocab.repeats(banks)
    if again:
        print("  " + again)
    if not a.no_play:
        try:
            for b, label, out in played:
                print("playing bank %d: %s" % (b, label))
                play_file(out)
        except audio.AudioError as exc:
            sys.exit(str(exc))
    jsonout.result(words=[dict(bank=b, word=label, file=out) for b, label, out in played],
                   played=not a.no_play, work_dir=work)
    print("\ntry it on the instrument with:  mfreak speech banks --from %s" % a.vocab)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("item", nargs="?", metavar="TEXT|AUDIO",
                    help="text to speak, or an audio file. Leave out to record")
    ap.add_argument("--from", dest="vocab", metavar="FILE",
                    help="play every word in a vocabulary file, bank by bank")
    ap.add_argument("--seconds", type=float, default=3.0,
                    help="how long to record (default: 3)")
    ap.add_argument("--voice", default=C.DEFAULT_VOICE,
                    help="system voice for text (default: %(default)s)")
    ap.add_argument("--device", metavar="NAME|N",
                    help="audio input, by name or number (default: the system "
                         "default)")
    ap.add_argument("--note", type=int, metavar="MIDI",
                    help="play it at this MIDI note, as the instrument "
                         "would (60 is the engine's base pitch)")
    ap.add_argument("--out-dir", default=None,
                    help="where to save the recording and the result")
    ap.add_argument("--no-play", action="store_true",
                    help="write files but do not play them")
    ap.add_argument("--image", default=None,
                    help="take the excitation pulse from this firmware "
                         "image. Not needed for normal use")
    a = ap.parse_args()

    work = a.out_dir or tempfile.mkdtemp(prefix="preview-")
    os.makedirs(work, exist_ok=True)
    lut = R.excitation_lut(bytearray(open(a.image, "rb").read())
                           if a.image else None)
    shift = 1.0 if a.note is None else 2.0 ** ((a.note - 60) / 12.0)
    if a.vocab:
        if a.item:
            ap.error("give TEXT|AUDIO or --from, not both")
        return preview_file(a, work, lut, shift)
    if a.item is None:
        src = record(a.seconds, os.path.join(work, "recorded.wav"), a.device)
        say = os.path.abspath(src)
    elif os.path.isfile(a.item):
        src = say = a.item
    else:
        src, say = C.tts(a.item, a.voice, work, 0), a.item

    # The same path the upload takes: load at 8 kHz, analyse, quantise into the
    # firmware's own LUTs. What comes out is byte-for-byte the device's frames.
    x = A.load_audio(src)
    frames = A.analyse(x, quantise=True)
    if not frames:
        sys.exit("no frames -- the recording is silent, or the input device is wrong")
    report(frames)

    y = R.synth(frames, lut, pitch_shift=shift)
    out = os.path.join(work, "resynthesised.wav")
    write_wav(out, y, 8000)
    print("\n  source        %s" % src)
    print("  through LPC   %s   (%.2f s%s)"
          % (out, len(y) / 8000.0,
             "" if a.note is None else ", note %d" % a.note))
    if not a.no_play:
        print("\nplaying the source, then what the engine makes of it")
        try:
            play_file(src)
            play_file(out)
        except audio.AudioError as exc:
            sys.exit(str(exc))
    # The same criteria report() prints, so the object and the prose can
    # never disagree: unvoiced is period == 0, silent is energy == 0.
    unvoiced = sum(1 for f in frames if f["period"] == 0)
    silent = sum(1 for f in frames if not f["energy"])
    jsonout.result(
        source=src, resynthesised=out, seconds=round(len(y) / 8000.0, 3),
        note=a.note, frames=len(frames), voiced=len(frames) - unvoiced,
        unvoiced=unvoiced, silent=silent,
        played=not a.no_play, work_dir=work,
        next="mfreak speech say %s" % say)
    print("\nput it on the instrument with:  mfreak speech say %s" % repr(say))
    return 0


if __name__ == "__main__":
    sys.exit(main())
