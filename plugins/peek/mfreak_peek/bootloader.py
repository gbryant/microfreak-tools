#!/usr/bin/env python3
"""Read your instrument's bootloader out of flash, over MIDI.

    mfreak fw patch peek stock.mff --out peek.mff
    mfreak fw flash peek.mff
    mfreak peek dump-bootloader --out bootloader.bin
    mfreak fw flash stock.mff

Arturia's firmware files do not include the 48 KB bootloader, so this is the
only way to get a copy. It needs the peek build installed. It only reads,
and writes nothing to the instrument.

The file contains your instrument's serial number. Do not publish it, attach
it to a bug report, or commit it. --redact-serial zeroes the serial; the rest
of the factory data page is written as read.

microfreak-emu boots the real bootloader from this file: put it in that
repository's data/bootloader/ folder.
"""
import argparse
import random
import sys
import time

from microfreak.firmware.peek import peek_reply
from mfreak_peek.build import peek_message_nibble
from microfreak.midi import open_synth, checksum_free_seq
from microfreak import jsonout

BASE = 0x08000000
SIZE = 48 << 10                 # sectors 0, 1 and 2
WORDS = SIZE // 4

# The factory data page, and the serial's offset inside it. Measured: the page
# starts at 0x08008000 and the serial is its first record -- sixteen ASCII
# digits that fill the field exactly, with the NUL terminator after it at
# +0x10, outside the field and outside what `redact` touches.
DATA_PAGE = 0x08008000
SERIAL_OFF, SERIAL_LEN = 0x00, 0x10


def peek_word(synth, addr, seqgen, tries=4):
    """-> the 32-bit word at `addr`, or None.

    A blocked or never-dispatched peek STILL REPLIES, carrying whatever the
    last successful one left in the septet slots, and the sequence number does
    not catch it -- the dispatcher refreshes seq from the incoming message, so
    a stale reply passes a seq check. That is why the dump is verified by
    re-reading a sample afterwards rather than trusted request by request.
    """
    for _ in range(tries):
        seq = next(seqgen)
        synth.send(list(peek_message_nibble(addr, seq=seq)))
        try:
            reply = synth.recv(
                lambda m: peek_reply(bytes(m), seq=seq) is not None, timeout=2.0)
        except TimeoutError:
            continue
        word = peek_reply(bytes(reply), seq=seq)
        if word is not None:
            return word
    return None


def looks_like_a_vector_table(words):
    """Is this actually the bootloader? -> (ok, why).

    Three structural facts, all of them true of any Cortex-M image and none of
    them true of a buffer of zeros, a buffer of 0xFF or a stale-reply smear:
    the initial SP points into this chip's SRAM, the reset vector points into
    the low 48 KB with the Thumb bit set, and the two differ.
    """
    if len(words) < 2:
        return False, "no data"
    sp, reset = words[0], words[1]
    if not 0x20000000 <= sp <= 0x20020000:
        return False, ("initial SP reads 0x%08X, which is not SRAM on this "
                       "part -- the reads are not returning flash" % sp)
    if not (BASE <= (reset & ~1) < BASE + SIZE):
        return False, ("reset vector reads 0x%08X, which is not inside the "
                       "bootloader" % reset)
    if not reset & 1:
        return False, ("reset vector 0x%08X has the Thumb bit clear, which no "
                       "Cortex-M image has" % reset)
    if len(set(words[:16])) < 4:
        return False, ("the first 16 vectors are nearly all the same value, "
                       "which is what a stale-reply smear looks like")
    return True, "SP 0x%08X, reset 0x%08X" % (sp, reset)


def redact(blob):
    """Zero the serial's sixteen bytes. -> (blob, what was there).

    Only those bytes. The rest of the factory data page is left as read.
    """
    off = DATA_PAGE - BASE + SERIAL_OFF
    was = bytes(blob[off:off + SERIAL_LEN])
    text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in was)
    blob[off:off + SERIAL_LEN] = b"\0" * SERIAL_LEN
    return blob, text


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, metavar="FILE",
                    help="where to write the 48 KB image")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port substring (default: MicroFreak)")
    ap.add_argument("--verify", type=int, default=256, metavar="N",
                    help="re-read N random words afterwards and compare; "
                         "0 skips it, which is not recommended (default: 256)")
    ap.add_argument("--redact-serial", action="store_true",
                    help="blank the serial number in the output")
    args = ap.parse_args()

    synth = open_synth(port_substring=args.port)
    seqgen = checksum_free_seq()
    print("reading 0x%08X..0x%08X (%d words) over MIDI" % (BASE, BASE + SIZE, WORDS))
    print("this needs the peek build: `mfreak fw patch peek`\n")

    blob = bytearray(SIZE)
    started = time.time()
    missing = []
    for i in range(WORDS):
        addr = BASE + i * 4
        word = peek_word(synth, addr, seqgen)
        if word is None:
            missing.append(addr)
            word = 0xFFFFFFFF
        blob[i * 4:i * 4 + 4] = word.to_bytes(4, "little")
        if i % 256 == 0 or i == WORDS - 1:
            done = i + 1
            rate = done / max(time.time() - started, 0.001)
            print("\r  %5d/%d words  %5.1f%%  %4.0f words/s  %d failed"
                  % (done, WORDS, 100.0 * done / WORDS, rate, len(missing)),
                  end="", flush=True)
    print()

    words = [int.from_bytes(blob[k:k + 4], "little") for k in range(0, 64, 4)]
    ok, why = looks_like_a_vector_table(words)
    print("\nvector table: %s  (%s)" % ("plausible" if ok else "WRONG", why))
    if not ok:
        print("\nRefusing to write it. Nothing here is a bootloader.", file=sys.stderr)
        return 1
    if missing:
        print("%d words never answered and are 0xFFFFFFFF in the file: %s%s"
              % (len(missing), ", ".join("0x%08X" % a for a in missing[:6]),
                 " ..." if len(missing) > 6 else ""))

    if args.verify:
        n = min(args.verify, WORDS)
        print("\nverifying %d random words" % n)
        bad = []
        for addr in sorted(random.sample(range(WORDS), n)):
            again = peek_word(synth, BASE + addr * 4, seqgen)
            first = int.from_bytes(blob[addr * 4:addr * 4 + 4], "little")
            if again is not None and again != first:
                bad.append((BASE + addr * 4, first, again))
        if bad:
            print("  %d of %d disagreed on a re-read:" % (len(bad), n))
            for a, f, s in bad[:8]:
                print("    0x%08X  0x%08X -> 0x%08X" % (a, f, s))
            print("\nRe-reads disagree, so the dump cannot be trusted. "
                  "Not writing it.", file=sys.stderr)
            return 1
        print("  all %d agreed" % n)

    if args.redact_serial:
        blob, was = redact(blob)
        # Do NOT echo the serial. The whole point of this flag is a file that
        # is safe to share, and the terminal is the thing people paste into
        # bug reports. Report the length so it is clear something was there.
        blanked = sum(1 for c in was if c != ".")
        print("\nserial field blanked: %d bytes at 0x%08X+0x%X, %d of them "
              "printable."
              % (SERIAL_LEN, DATA_PAGE, SERIAL_OFF, blanked))
    else:
        print("\n*** This file contains your instrument's SERIAL NUMBER, in the")
        print("*** factory data page at 0x%08X. Do not publish it. Re-run with" % DATA_PAGE)
        print("*** --redact-serial to zero those sixteen bytes.")

    with open(args.out, "wb") as fh:
        fh.write(bytes(blob))
    # A caller has to know whether the file it now holds carries the serial.
    jsonout.result(out=args.out, bytes=len(blob),
                   serial_redacted=bool(args.redact_serial),
                   contains_serial=not args.redact_serial,
                   do_not_publish=not args.redact_serial)
    print("\nwrote %s, %d bytes" % (args.out, len(blob)))
    print("Use it with microfreak-emu:  cp %s <microfreak-emu>/data/bootloader/"
          % args.out)
    print("Then put your own firmware back on the instrument.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
