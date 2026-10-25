#!/usr/bin/env python3
"""List every wavetable by name.

    mfreak wavetable list
    mfreak wavetable list --from 9 --to 16

Empty wavetables are listed as (empty).
"""
import argparse
import sys

from microfreak import jsonout
from microfreak import panel
from microfreak.listing import Listing
from microfreak.midi import checksum_free_seq, open_synth
from microfreak.wavetable.read import is_empty, pull_block, wavetable_name_of


def read_name(synth, slot, seqgen):
    """-> the wavetable's name, or None if the slot is empty."""
    block = pull_block(synth, 0x57, slot, 0, seqgen)
    return None if is_empty(block) else wavetable_name_of(block)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        epilog="Read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="first", type=int, default=1,
                    metavar="WAVETABLE", help="first wavetable (default: 1)")
    ap.add_argument("--to", dest="last", type=int, default=panel.WAVETABLES,
                    metavar="WAVETABLE",
                    help="last wavetable, inclusive (default: 16)")
    ap.add_argument("--all", action="store_true",
                    help="print every wavetable, without collapsing empty runs")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    first = panel.to_slot(ap, args.first, panel.WAVETABLES, "--from wavetable")
    last = panel.to_slot(ap, args.last, panel.WAVETABLES, "--to wavetable")
    if last < first:
        ap.error("--to must not be below --from")

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    rows = []
    out = Listing("wavetable  name", "wavetable", last + 1, collapse=not args.all,
                  span=9)
    try:
        for slot in range(first, last + 1):
            r = dict(slot=slot, panel=slot + 1,
                     name=read_name(synth, slot, seqgen))
            rows.append(r)
            if r["name"] is not None:
                out.add(r["panel"], "%9d  %s" % (r["panel"], r["name"]))
            else:
                out.add(r["panel"], "%9d  (empty)" % r["panel"],
                        key="(empty)", label="(empty)")
    finally:
        synth.close()
        out.finish()

    used = sum(1 for r in rows if r["name"] is not None)
    print("\n%d of %d in use" % (used, len(rows)))
    jsonout.result(first=first, last=last, in_use=used, wavetables=rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
