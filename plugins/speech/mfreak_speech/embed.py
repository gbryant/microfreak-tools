#!/usr/bin/env python3
"""Put your own words in a firmware file, in place of the built-in ones.

    mfreak speech embed stock.mff --from vocab.txt --out words.mff
    mfreak speech embed speech.mff --bank hello goodbye --out speech-words.mff

Every bank is wiped, then yours are written. A Wave position you leave out
repeats the nearest bank below it, and takes no extra space. The file can be the stock 5.0.0.2084 .mff, a build from
`mfreak fw patch`, or a file this command made. Stock with your words reads
PATCHED words1 on the boot screen. To keep `speech say` working as well,
embed into a speech build.

This writes a file. `mfreak fw flash` puts it on the instrument.
"""
import argparse
import sys
import tempfile

import microfreak.lpc as S
from microfreak.firmware import image as I
from microfreak.firmware import patching as F
import mfreak_speech.cli as C
import mfreak_speech.vocab as vocab

FLAVOUR, REV, LETTER = "words", 2, "W"


def kind(img):
    """-> "stock", "words" or "patched", or exit for anything else."""
    if F.payload_sha256(img) == F.KNOWN_PAYLOAD_SHA256:
        return "stock"
    line1 = F.banner(img)
    if line1 is None:
        sys.exit("refusing: this is not stock 5.0.0.2084 or a build `mfreak fw "
                 "patch` made")
    return "words" if line1.startswith("PATCHED " + FLAVOUR) else "patched"


def room(what):
    """-> where the banks may go. A patch's code sits at the top, and
    stock with words keeps only its identity handler there."""
    end = F.CODE_BASE if what == "patched" else F.WHO_AT
    return F.VOCAB_START, end


def build(banks):
    """Five lists of word frames -> five bitstreams, b"" where empty.
    Exits if a bank breaks the firmware's limits."""
    out, problems = [], []
    for i, words in enumerate(banks):
        n = sum(len(w) for w in words)
        if len(words) > S.MAX_WORDS:
            problems.append("bank %d has %d words; the limit is %d"
                            % (i + 1, len(words), S.MAX_WORDS))
        if n > S.MAX_FRAMES:
            problems.append("bank %d is %.1f s; the limit is %.1f s per bank"
                            % (i + 1, n / S.FPS, S.MAX_FRAMES / S.FPS))
        out.append(S.encode_bank(words) if words else b"")
    if problems:
        sys.exit("that will not fit:\n  " + "\n  ".join(problems))
    return out


def layout(blobs, start, end):
    """-> [(ptr, size)] per bank, packed from `start`, 4-byte aligned, and
    the bytes used. An empty bank shares the entry of the bank it repeats."""
    descs, addr = [], start
    for b in blobs:
        if not b:
            descs.append(None)
            continue
        descs.append((addr, len(b)))
        addr = (addr + len(b) + 3) & ~3
    descs = [descs[s] for s in vocab.sources(blobs)]
    return descs, addr - start


def report(banks, blobs, used, capacity):
    print("\nbank  words  seconds   bytes")
    src = vocab.sources(banks)
    for i, (words, blob) in enumerate(zip(banks, blobs)):
        if words:
            print("%-4d  %5d  %7.1f  %6d" % (i + 1, len(words),
                                             sum(map(len, words)) / S.FPS, len(blob)))
        else:
            print("%-4d      -        -       -   repeats bank %d" % (i + 1, src[i] + 1))
    secs = sum(len(w) for b in banks for w in b) / S.FPS
    rate = used / secs if secs else 0
    print("total %5d  %7.1f  %6d of %d bytes (%.0f%%)"
          % (sum(map(len, banks)), secs, used, capacity, 100.0 * used / capacity))
    if used <= capacity and rate:
        print("room for about %.0f s more at the rate of these words"
              % ((capacity - used) / rate))
    return rate


def embed(img, blobs, start, end):
    """Wipe every bank in `img` and write `blobs` in their place."""
    descs, _ = layout(blobs, start, end)
    img[start - F.BASE:end - F.BASE] = bytes(end - start)
    for (ptr, size), blob in zip(descs, blobs):
        if blob:
            img[ptr - F.BASE:ptr - F.BASE + size] = blob
    F.set_word_banks(img, descs)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]) + "\n\n" + C.VOCAB_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", metavar="FILE",
                    help="the stock .mff, or a build from `fw patch` or `embed`")
    C.add_words(ap)
    ap.add_argument("--out", metavar="OUT.mff",
                    help="the firmware file to write")
    ap.add_argument("--voice", default=C.DEFAULT_VOICE,
                    help="system voice for text (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show how much space it needs, without writing")
    a = ap.parse_args()
    if not a.out and not a.dry_run:
        ap.error("--out is required, unless --dry-run")

    img = I.main_code(a.image)
    what = kind(img)
    start, end = room(what)
    items = C.words_from(a)
    print("preparing:")
    with tempfile.TemporaryDirectory() as td:
        banks = C.analyse_banks(items, a.voice, td)
    blobs = build(banks)
    _, used = layout(blobs, start, end)
    capacity = end - start
    rate = report(banks, blobs, used, capacity)
    if used > capacity:
        over = used - capacity
        sys.exit("\nthat will not fit: %d bytes over%s. Shorten or drop some "
                 "words." % (over, ", about %.0f s of speech" % (over / rate)
                             if rate else ""))
    again = vocab.repeats(banks)
    if a.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    embed(img, blobs, start, end)
    if what != "patched":
        if what == "stock":
            F.stamp_version(img, FLAVOUR, REV)
        F.install_who(img, LETTER, F.banner_rev(img))
        print("\nbanner    the boot screen reads %s" % F.banner(img))
    print()
    F.write(img, a.out, source=a.image)
    if again:
        print(again)
    return 0


if __name__ == "__main__":
    sys.exit(main())
