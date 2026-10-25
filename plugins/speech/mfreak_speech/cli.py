#!/usr/bin/env python3
"""Put your own words on the instrument's Speech engine.

    mfreak speech say "I am micro freak"
    mfreak speech banks --bank "hello there" \\
                        --bank one two three four five \\
                        --bank goodbye

Each item is either text, spoken by the system voice, or the path to an
audio file, such as a recording of your own voice.

Needs firmware patched with `mfreak fw patch speech`. Before sending, load a Speech
preset and play a note until you hear a word; the command prompts you to
unless --yes. The words
stay until the instrument is switched off.
"""
import argparse
import os
import shutil
import subprocess
import sys
import contextlib
import tempfile

import mfreak_speech.sysex as X  # noqa: E402
import mfreak_speech.vocab as vocab  # noqa: E402
import microfreak.lpc as S  # noqa: E402

DEFAULT_VOICE = "Samantha"


# ---------------------------------------------------------------- input
def tts(text, voice, outdir, index):
    """Speak `text` to an audio file. macOS `say` for now."""
    if not shutil.which("say"):
        sys.exit("no text-to-speech on this machine (`say` not found).\n"
                 "Pass audio files instead of text, or record what you want:\n"
                 "    mfreak speech banks --bank myvoice.wav")
    path = os.path.join(outdir, "tts%02d.aiff" % index)
    r = subprocess.run(["say", "-v", voice, "-o", path, text],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("`say -v %s` failed: %s\nTry `mfreak doctor` for the voices "
                 "this machine has." % (voice, (r.stderr or "").strip()))
    return path


def resolve(items, voice, outdir):
    """-> [(label, audio path)] for vocab items, ("audio", path) or ("text", str).
    Text is spoken by the system voice."""
    out = []
    for kind, item in items:
        if kind == "audio":
            out.append((os.path.basename(item), item))
        else:
            out.append((item, tts(item, voice, outdir, len(out))))
    return out


def word_frames(path, label):
    """-> one word's frames, as both the upload and the flash path need them:
    leading silence removed, loudness matched to the factory words, and a
    silent last frame so a held key falls quiet."""
    import mfreak_speech.analyze as A
    fr = S.trim_leading_silence(A.analyse(A.load_audio(path), quantise=True))
    if not fr:
        sys.exit("%s: silent, so there is nothing to put on the instrument" % label)
    return S.end_with_silence(S.match_device_range(fr))


# ---------------------------------------------------------------- checks
def analyse_banks(banks, voice, outdir, quiet=False):
    """Five lists of vocab items -> five lists of word frames, printing each."""
    done = []
    for b, items in enumerate(banks):
        frames = []
        for label, path in resolve(items, voice, outdir):
            fr = word_frames(path, label)
            frames.append(fr)
            if not quiet:
                print("  bank %d  %-28s %3d frames  %4.1f s"
                      % (b + 1, label[:28], len(fr), len(fr) / S.FPS))
        done.append(frames)
    return done


def preflight(banks):
    """Refuse what the firmware cannot hold, in words rather than hex."""
    problems = []
    for i, frames in enumerate(banks):
        words, n = len(frames), sum(len(f) for f in frames)
        if words > X.MAX_WORDS_PER_BANK:
            problems.append(
                "bank %d has %d items; the limit is %d per bank. Split it across "
                "two banks." % (i + 1, words, X.MAX_WORDS_PER_BANK))
        secs = n / S.FPS
        if n * S.FRAME_BYTES >= X.MAGIC_OFF:
            problems.append("bank %d is %.1f s, which cannot fit at all." % (i + 1, secs))
    # the real constraint couples the two budgets
    full = [b for b in banks if b]
    if full:
        longest = max(sum(len(f) for f in b) for b in full)
        streams = sum(len(S.encode_bank(b)) for b in full)
        need = longest * S.FRAME_BYTES + streams
        if need > X.MAGIC_OFF:
            over = need - X.MAGIC_OFF
            problems.append(
                "all of it together needs %d bytes of the %d available, %d too "
                "many.\n  The longest bank (%.1f s) sets aside room to decode "
                "into, so shortening THAT frees the most."
                % (need, X.MAGIC_OFF, over, longest / S.FPS))
    if problems:
        sys.exit("that will not fit:\n\n  " + "\n\n  ".join(problems))


def synth_or_die():
    """-> an open Synth, or the emulator if MFREAK_EMULATOR is set.

    This used to be `port_or_die`, which enumerated rtmidi2 itself and
    returned a port NAME for `send` to re-open. Two consequences, both
    contradicting what capabilities.RULES and the README promise: `speech say`
    and `speech banks` did not honour MFREAK_EMULATOR, and the name was
    matched case-sensitively on "MicroFreak" where `Synth._find` is
    case-insensitive -- so a port the rest of the tool finds, this missed.
    """
    from microfreak.midi import open_synth
    return open_synth()


def check_firmware(synth):
    """Stop unless the instrument reports the speech patch."""
    from microfreak import plugin
    reply = plugin.inquire(synth)
    if reply is None:
        sys.exit("the instrument did not answer when asked for its version. "
                 "Run `mfreak doctor`.")
    what, _ = plugin.running(reply)
    if what == "speech":
        return
    if what == "stock":
        runs = "stock firmware"
    elif what == "official":
        runs = "official firmware %d.%d.%d" % tuple(reply[12:15])
    else:
        runs = "the %s patch" % what if what else "another patched build"
    sys.exit("this instrument runs %s, which ignores speech uploads. Flash the "
             "speech patch first:\n"
             "    mfreak fw patch speech <the stock 5.0.0.2084 .mff> --out speech.mff\n"
             "    mfreak fw flash speech.mff" % runs)


def wait_for_preset(skip):
    if skip:
        return
    print("\nOn the MicroFreak: select a Speech preset and play a note until "
          "you hear a word.")
    try:
        input("Press Enter when you have... ")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\naborted.")


# ---------------------------------------------------------------- commands
def where_words_are(banks):
    """-> lines telling the player where the words are, and what repeats."""
    used = [i + 1 for i, b in enumerate(banks) if b]
    again = vocab.repeats(banks)
    if len(used) == 1:
        lines = ["Shape picks the word. Bank %d is Wave position %d; bank 1 is "
                 "the lowest Wave\nsetting that speaks words, around 40."
                 % (used[0], used[0]) if used[0] != 1 else
                 "Shape picks the word. Set Wave to the lowest setting that "
                 "speaks words,\naround 40."]
    else:
        lines = ["Wave picks the bank, Shape the word in it. Bank 1 is the "
                 "lowest Wave\nsetting that speaks words, around 40."]
    if again:
        lines.append(again)
    if used[0] == 1:
        lines.append("For paraphonic, turn Wave down into bank 1 from above.")
    return lines


def cmd_upload(banks_items, voice, skip_prompt, quiet, dry_run=False):
    synth = None if dry_run else synth_or_die()
    # ExitStack rather than a nested `with`: the port has to close on the way
    # out however this leaves, and `send` used to open a fresh MidiOut per
    # call and close none of them.
    with contextlib.ExitStack() as stack:
        td = stack.enter_context(tempfile.TemporaryDirectory())
        if synth is not None:
            stack.callback(synth.close)
            check_firmware(synth)
        print("preparing:")
        banks = analyse_banks(banks_items, voice, td, quiet)
        preflight(banks)
        uploads = []
        if banks[0]:
            uploads.append(("vocabulary", X.build_sentence(banks[0])[0]))
        table_writes, report, entries, start, total = X.table_upload(banks)
        uploads.append(("banks", table_writes))
        print()
        X.budget_report(report, entries, start, total)
        if dry_run:
            print("\n--dry-run: nothing sent.")
            return
        wait_for_preset(skip_prompt)
        print("\nsending to %s" % getattr(synth, "port_name", "the emulator"))
        for label, writes in uploads:
            send(label, writes, synth)
    print("\nDone. " + "\n".join(where_words_are(banks)))
    print("Switching the instrument off brings the factory words back.")


def send(label, writes, synth):
    """Send one upload, with a progress line that counts down."""
    import time
    msgs = X.messages(writes)
    for i, m in enumerate(msgs):
        if i:
            time.sleep(0.25)        # verified spacing; faster loses messages
        synth.send(list(m))
        print("\r  %-11s %3d/%d  %3.0f%%  %3.0f s left"
              % (label, i + 1, len(msgs), 100.0 * (i + 1) / len(msgs),
                 (len(msgs) - i - 1) * 0.25), end="", flush=True)
    print()


VOCAB_HELP = """\
A vocabulary file names a Wave position in brackets, then one word per line,
as text or an audio file relative to the file. A file with no headings is
bank 1. Blank lines and lines starting with # are skipped:

    [1]
    hello
    recordings/my-name.wav
    [3]
    goodbye"""


def add_words(p):
    """--bank and --from, one of them required."""
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bank", action="append", nargs="+", dest="banks",
                   metavar="WORD",
                   help="text or audio files for one Wave position. Repeatable")
    g.add_argument("--from", dest="vocab", metavar="FILE",
                   help="a vocabulary file, for all five Wave positions")


def words_from(a):
    """-> five lists of vocab items from --bank or --from."""
    return vocab.parse(a.vocab) if a.vocab else vocab.from_flags(a.banks)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = lambda p: (
        p.add_argument("--voice", default=DEFAULT_VOICE,
                       help="system voice for text (default: %(default)s)"),
        p.add_argument("--yes", action="store_true",
                       help="skip the prompt to load a Speech preset first"),
        p.add_argument("--quiet", action="store_true", help="print less"),
        p.add_argument("--dry-run", action="store_true",
                       help="show how much space it needs, without sending"))
    RAW = argparse.RawDescriptionHelpFormatter
    s1 = sub.add_parser(
        "say", help="put words on it, one per Shape position",
        formatter_class=RAW,
        description="Put words on the instrument. The Shape knob picks which "
                    "one a key plays.",
        epilog='    mfreak speech say "I am micro freak"\n'
               '    mfreak speech say hello goodbye "thank you"\n'
               "    mfreak speech say myvoice.wav\n\n"
               "Needs firmware patched with `mfreak fw patch speech`. The words "
               "stay until\nthe instrument is switched off.")
    s1.add_argument("words", nargs="+", metavar="WORD",
                    help="text to speak, or an audio file, for each Shape position")
    common(s1)
    s2 = sub.add_parser(
        "banks", help="one bank per Wave position", formatter_class=RAW,
        description="Upload one word bank per Wave knob position.",
        epilog='    mfreak speech banks --bank "hello there" --bank one two three\n'
               "    mfreak speech banks --from vocab.txt\n\n"
               "Each --bank is one Wave position, from 1. A position without "
               "words repeats\nthe nearest bank below it.\n" + VOCAB_HELP + "\n\nNeeds firmware patched "
               "with `mfreak fw patch speech`. The words stay until the\n"
               "instrument is switched off.")
    add_words(s2)
    common(s2)
    a = ap.parse_args()

    banks = vocab.from_flags([a.words]) if a.cmd == "say" else words_from(a)
    cmd_upload(banks, a.voice, a.yes, a.quiet, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
