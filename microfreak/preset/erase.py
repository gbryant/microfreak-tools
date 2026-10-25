#!/usr/bin/env python3
"""Empty presets on the instrument, as MCC does for unused slots.

    mfreak preset erase 300
    mfreak preset erase --from 100 --to 199
    mfreak preset erase --all

An empty preset reads back as Init. Presets that are already empty are
left alone.

This deletes the presets it empties, and ignores the instrument's Mem
protect setting. It lists them and asks first. --yes skips the question.
Back up first: `mfreak preset backup FILE.mfprojz`.
"""
import argparse
import sys

from microfreak import confirm
from microfreak import jsonout
from microfreak import panel
from microfreak.midi import checksum_free_seq, open_synth
from microfreak.preset.read import name_of, read_name_record
from microfreak.preset.write import erase_preset, slot_is_empty


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("preset", type=int, nargs="?", metavar="PRESET",
                    help="the preset to empty (1-512)")
    ap.add_argument("--from", dest="first", type=int, metavar="PRESET",
                    help="first preset to empty")
    ap.add_argument("--to", dest="last", type=int, metavar="PRESET",
                    help="last preset to empty, inclusive")
    ap.add_argument("--all", action="store_true",
                    help="empty all 512 presets")
    ap.add_argument("--yes", action="store_true",
                    help="empty them without asking")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    ranged = args.first is not None or args.last is not None
    if (args.preset is not None) + ranged + args.all != 1:
        ap.error("give one of PRESET, --from/--to, or --all")
    if args.preset is not None:
        first = last = panel.to_slot(ap, args.preset, panel.PRESETS, "preset")
    elif args.all:
        first, last = 0, panel.PRESETS - 1
    else:
        if args.first is None or args.last is None:
            ap.error("--from and --to go together")
        first = panel.to_slot(ap, args.first, panel.PRESETS, "--from preset")
        last = panel.to_slot(ap, args.last, panel.PRESETS, "--to preset")
        if last < first:
            ap.error("--to must not be below --from")

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    erased, failed = [], []
    interrupted = False
    try:
        targets = []
        for slot in range(first, last + 1):
            record = read_name_record(synth, slot, seqgen)
            if not slot_is_empty(record):
                targets.append((slot, name_of(record)))
        for slot, name in targets:
            print("  %3d  %s" % (slot + 1, name))
        if not targets:
            print("already empty")
            jsonout.result(empty=[], erased=[], failed=[], dry_run=False)
            return 0
        if not confirm.ask("Empty %d preset%s?" % (
                len(targets), "" if len(targets) == 1 else "s"), args.yes):
            jsonout.result(empty=[s + 1 for s, _ in targets], erased=[],
                           failed=[], dry_run=True)
            return 0

        try:
            for slot, _ in targets:
                try:
                    erase_preset(synth, slot, seqgen)
                    ok = slot_is_empty(read_name_record(synth, slot, seqgen))
                except (TimeoutError, RuntimeError) as exc:
                    failed.append(dict(panel=slot + 1, error=str(exc)))
                    print("  %3d  FAILED: %s" % (slot + 1, exc), file=sys.stderr)
                    continue
                if not ok:
                    failed.append(dict(panel=slot + 1, error="still holds a preset"))
                    print("  %3d  FAILED: still holds a preset" % (slot + 1),
                          file=sys.stderr)
                    continue
                erased.append(slot + 1)
                print("  emptied preset %3d" % (slot + 1), file=sys.stderr)
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
