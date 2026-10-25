#!/usr/bin/env python3
"""Empty wavetables on the instrument, as MCC does.

    mfreak wavetable erase 16
    mfreak wavetable erase --from 13 --to 16

Wavetables that are already empty are left alone.

This deletes the wavetables it empties. Save them first with
`wavetable backup F.mfwbz` if you want them back. It lists them and asks
first. --yes skips the question.
"""
import argparse
import sys

from microfreak import confirm
from microfreak import jsonout
from microfreak import panel
from microfreak.midi import checksum_free_seq, open_synth
from microfreak.wavetable.listing import read_name
from microfreak.wavetable.write import build_name_chunk, write_name


def erase_wavetable(synth, slot, seqgen):
    """Flag a slot empty with a blank name chunk. MCC also rewrites the four
    frames with a placeholder (captures/wavetable-1-6--10-13.pcapng); the
    flag alone empties the slot, and the old frames stay."""
    write_name(synth, slot, build_name_chunk(slot, "", empty=True), seqgen)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wavetable", type=int, nargs="?", metavar="WAVETABLE",
                    help="the wavetable to empty (1-16)")
    ap.add_argument("--from", dest="first", type=int, metavar="WAVETABLE",
                    help="first wavetable to empty")
    ap.add_argument("--to", dest="last", type=int, metavar="WAVETABLE",
                    help="last wavetable to empty, inclusive")
    ap.add_argument("--yes", action="store_true",
                    help="empty them without asking")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    ranged = args.first is not None or args.last is not None
    if (args.wavetable is not None) + ranged != 1:
        ap.error("give WAVETABLE, or --from and --to")
    if args.wavetable is not None:
        first = last = panel.to_slot(ap, args.wavetable, panel.WAVETABLES,
                                     "wavetable")
    else:
        if args.first is None or args.last is None:
            ap.error("--from and --to go together")
        first = panel.to_slot(ap, args.first, panel.WAVETABLES, "--from wavetable")
        last = panel.to_slot(ap, args.last, panel.WAVETABLES, "--to wavetable")
        if last < first:
            ap.error("--to must not be below --from")

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    erased, failed = [], []
    interrupted = False
    try:
        targets = []
        for slot in range(first, last + 1):
            name = read_name(synth, slot, seqgen)
            if name is not None:
                targets.append((slot, name))
        for slot, name in targets:
            print("  %2d  %s" % (slot + 1, name))
        if not targets:
            print("already empty")
            jsonout.result(empty=[], erased=[], failed=[], dry_run=False)
            return 0
        if not confirm.ask("Delete %d wavetable%s?" % (
                len(targets), "" if len(targets) == 1 else "s"), args.yes):
            jsonout.result(empty=[s + 1 for s, _ in targets], erased=[],
                           failed=[], dry_run=True)
            return 0

        try:
            for slot, _ in targets:
                try:
                    erase_wavetable(synth, slot, seqgen)
                    name = read_name(synth, slot, seqgen)
                except (TimeoutError, RuntimeError) as exc:
                    failed.append(dict(panel=slot + 1, error=str(exc)))
                    print("  %2d  FAILED: %s" % (slot + 1, exc), file=sys.stderr)
                    continue
                if name is not None:
                    failed.append(dict(panel=slot + 1, error="still holds a wavetable"))
                    print("  %2d  FAILED: still holds a wavetable" % (slot + 1),
                          file=sys.stderr)
                    continue
                erased.append(slot + 1)
                print("  emptied wavetable %2d" % (slot + 1), file=sys.stderr)
        except KeyboardInterrupt:
            interrupted = True
            print("\nstopped", file=sys.stderr)
    finally:
        synth.close()

    print("emptied %d of %d" % (len(erased), len(targets)))
    if failed:
        print("error: %d failed: %s"
              % (len(failed), ", ".join(str(f["panel"]) for f in failed)),
              file=sys.stderr)
    jsonout.result(empty=[s + 1 for s, _ in targets], erased=erased,
                   failed=failed, dry_run=False, interrupted=interrupted)
    if interrupted:
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
