#!/usr/bin/env python3
"""Save the instrument's wavetables as an MCC wavetable bank.

    mfreak wavetable backup mine.mfwbz
    mfreak wavetable backup some.mfwbz --from 9 --to 16

The file is an MCC wavetable bank (.mfwbz). Each wavetable keeps its number
and name, and empty ones are saved as empty. The file is read back and
checked after it is written. Restore it with `wavetable restore mine.mfwbz`.
"""
import argparse
import os
import sys

import microfreak.bank as B
from microfreak import jsonout
from microfreak import mccfile
from microfreak import panel
from microfreak.midi import checksum_free_seq, open_synth
from microfreak.wavetable.read import read_wavetable


def read_slots(synth, seqgen, slots):
    """-> ({slot: (name, pcm) or None when empty}, [{panel, error}])."""
    got, failed = {}, []
    for slot in slots:
        try:
            name, frames = read_wavetable(synth, slot, seqgen)
        except (TimeoutError, RuntimeError) as exc:
            failed.append(dict(panel=slot + 1, error=str(exc)))
            print("wavetable %d FAILED: %s" % (slot + 1, exc), file=sys.stderr)
            continue
        if name is None:
            got[slot] = None
            continue
        if any(f is None for f in frames):
            failed.append(dict(panel=slot + 1, error="frames missing"))
            print("wavetable %d FAILED: frames missing" % (slot + 1),
                  file=sys.stderr)
            continue
        got[slot] = (name, mccfile.wavetable_pcm(frames))
        print("  wavetable %2d  %s" % (slot + 1, name), file=sys.stderr)
    return got, failed


def empty_archive(name):
    """An unused wavetable slot, as MCC writes one: no audio."""
    return B.Archive(version=mccfile.AUDIO_VERSION, name=name,
                     tags=B.tag_string(), values=[], scalars=("1", "0", "0"),
                     abc=("1", "0", "1"))


def save_bank(path, tables):
    """Write {slot: (name, pcm) or None} as a .mfwbz and read it back.
    -> the wavetables that did not read back identically, by panel number."""
    bank = os.path.splitext(os.path.basename(path))[0] or "Backup"
    items = [(slot + 1, empty_archive("%s-A%d" % (bank, slot + 1)) if t is None
              else mccfile.audio_archive(t[0], t[1], "wavetable"))
             for slot, t in sorted(tables.items())]
    B.write_bank(path, items, top=bank, subdir="01-%s-A" % bank)
    back = {it.slot: it for it in B.read_bank(path)}
    return [slot + 1 for slot, t in tables.items()
            if slot + 1 not in back
            or (not back[slot + 1].is_empty() if t is None
                else back[slot + 1].name != t[0] or back[slot + 1].pcm() != t[1])]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]) + "\nRead-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", metavar="FILE.mfwbz", help="the bank to write")
    ap.add_argument("--from", dest="first", type=int, default=1,
                    metavar="WAVETABLE", help="first wavetable (default: 1)")
    ap.add_argument("--to", dest="last", type=int, default=panel.WAVETABLES,
                    metavar="WAVETABLE",
                    help="last wavetable, inclusive (default: 16)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite the file if it exists")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    first = panel.to_slot(ap, args.first, panel.WAVETABLES, "--from wavetable")
    last = panel.to_slot(ap, args.last, panel.WAVETABLES, "--to wavetable")
    if last < first:
        ap.error("--to must not be below --from")
    try:
        mccfile.check_ext(args.out, "wavetable", bank=True)
    except mccfile.FileError as exc:
        ap.error(str(exc))
    if os.path.exists(args.out) and not args.force:
        sys.exit("%s already exists. --force overwrites it." % args.out)

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        tables, failed = read_slots(synth, seqgen, range(first, last + 1))
    finally:
        synth.close()

    if not tables:
        sys.exit("no wavetables were read, so nothing was written")
    unreadable = save_bank(args.out, tables)
    empty = sum(1 for t in tables.values() if t is None)
    print("\n%d wavetables -> %s" % (len(tables) - empty, args.out))
    if empty:
        print("%d empty saved as empty" % empty)
    if unreadable:
        print("error: %d did not read back from the file: %s"
              % (len(unreadable), unreadable), file=sys.stderr)
    if failed:
        print("error: %d could not be read: %s"
              % (len(failed), ", ".join(str(f["panel"]) for f in failed)),
              file=sys.stderr)
    jsonout.result(out=os.path.abspath(args.out), count=len(tables) - empty,
                   empty=empty, failed=failed, unreadable=unreadable)
    return 1 if (failed or unreadable) else 0


if __name__ == "__main__":
    sys.exit(main())
