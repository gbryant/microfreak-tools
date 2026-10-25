#!/usr/bin/env python3
"""List every preset by name. Takes under a second.

    mfreak preset list                     all 512
    mfreak preset list --from 1 --to 64    the first bank
    mfreak preset list --all               do not collapse runs of empty presets
    mfreak preset list --named             hide empty presets

Empty presets are listed as (empty). A preset you saved as Init is listed
by its name.
"""
import argparse
import sys

from microfreak.midi import open_synth, checksum_free_seq
from microfreak.preset.read import read_name_record, name_of
from microfreak.preset.write import slot_is_empty
from microfreak import jsonout
from microfreak.listing import Listing
from microfreak import panel

# Kept because microfreak/preset/backup.py imports it, and because `sample
# listing` has its own LAST_SLOT doing real work. Nothing in THIS module uses
# it any more: the range arguments became panel numbers and bound against
# panel.PRESETS instead.
LAST_SLOT = 511
EMPTY = "(empty)"


def read_names(synth, first, last, seqgen, on_slot=None):
    """-> [(slot, name)] for first..last inclusive, with name EMPTY for an
    empty slot.

    A slot that does not answer is recorded as None rather than aborting the
    run: an inventory of 512 slots is worth having with a hole in it, and a
    hole is visible in the output.
    """
    out = []
    for slot in range(first, last + 1):
        try:
            record = read_name_record(synth, slot, seqgen)
            name = EMPTY if slot_is_empty(record) else name_of(record)
        except TimeoutError:
            name = None
        out.append((slot, name))
        if on_slot:
            on_slot(slot, name)
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        epilog="Read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="first", type=int, default=1,
                    metavar="PRESET", help="first preset (default: 1)")
    ap.add_argument("--to", dest="last", type=int, default=panel.PRESETS,
                    metavar="PRESET", help="last preset, inclusive (default: 512)")
    ap.add_argument("--all", action="store_true",
                    help="print every preset, without collapsing repeated names")
    ap.add_argument("--named", action="store_true",
                    help="hide empty presets")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    # Panel numbers in, 0-based slots out, before anything downstream
    # sees them. Inclusive at both ends either way.
    args.first = panel.to_slot(ap, args.first, panel.PRESETS, "--from preset")
    args.last = panel.to_slot(ap, args.last, panel.PRESETS, "--to preset")
    if args.last < args.first:
        ap.error("--to must not be below --from")

    # One number, and it is the one on the instrument. This printed a `panel`
    # column beside a `slot` column, which was the code admitting it had two
    # conventions and no answer about which a reader wanted. The 0-based index
    # is still in --json as `slot`, where a program can have it.
    if args.named:
        out = Listing("preset  name", "preset", args.last + 1, collapse=False)
    else:
        out = Listing("preset       name", "preset", args.last + 1,
                      collapse=not args.all)

    def show(slot, name):
        if args.named:
            if name is None or name == EMPTY:
                out.reading(slot + 1)
            else:
                out.add(slot + 1, "%6d  %s" % (slot + 1, name))
            return
        label = "(no reply)" if name is None else name
        out.add(slot + 1, "%6d       %s" % (slot + 1, label), key=label, label=label)

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        rows = read_names(synth, args.first, args.last, seqgen, on_slot=show)
    finally:
        synth.close()
        out.finish()

    missing = [s for s, n in rows if n is None]
    named = [(s, n) for s, n in rows if n is not None and n != EMPTY]
    empty = [s for s, n in rows if n == EMPTY]

    print("\n%d presets read, %d in use, %d empty%s"
          % (len(rows), len(named), len(empty),
             ", %d did not answer" % len(missing) if missing else ""))
    jsonout.result(
        first=args.first, last=args.last, count=len(rows),
        named=len(named), empty=len(empty), no_reply=missing,
        slots=[dict(slot=s, panel=s + 1,
                    name=None if n == EMPTY else n, empty=n == EMPTY)
               for s, n in rows])
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
