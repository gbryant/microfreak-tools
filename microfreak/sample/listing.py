#!/usr/bin/env python3
"""List every sample's name and length. Takes about 12 s.

    mfreak sample list              all 128
    mfreak sample list --used       only those holding a sample
    mfreak sample list --to 16      the first 16

It ends with how much sample memory is used and free, matching what MIDI
Control Center shows. Total capacity is about 210 s, not the advertised 219.

The instrument cannot send sample audio back, only names and lengths. The
file you uploaded is the only copy, so keep it before overwriting a sample.
Names are at most 12 characters.
"""

# Evidence:
# - Used space is read off the instrument, not summed: the records form one
#   chain from 0x281000, so used = last record's address + its page-rounded
#   size. Summing audio lengths misses headers and rounding (123 KB here).
# - Free space is measured against SAMPLE_REGION_END, where the firmware's
#   boot-time scan stops (microfreak-emu). MCC agrees: 124.4 s used and
#   85.5 s free in MCC, 124.4 and 85.4 here. MCC subtracts rounded figures.
# - Names read back cut to 12 characters: of 52 names in MCC's export of the
#   same library, the only two longer than 12 came back truncated. Whether
#   the device stores 12 or only returns 12 is unknown.

import argparse
import sys

from microfreak.midi import open_synth, checksum_free_seq
from microfreak.sample.write import read_slot_record
from microfreak.sample.encode import TARGET_RATE
from microfreak import jsonout
from microfreak import panel
from microfreak.listing import Listing

LAST_SLOT = 127
BYTES_PER_FRAME = 2          # 16-bit mono
PAGE = 0x1000

# The sample region of the external flash. The base is confirmed on hardware
# -- the first record sits exactly there -- and the end is microfreak-emu's
# reading of where the firmware's boot scan stops, not a measurement. See the
# module docstring before quoting free space as exact.
SAMPLE_REGION_BASE = 0x281000
SAMPLE_REGION_END = 0xF50000


def allocated(audio_bytes):
    """-> the flash a record of this size occupies, header and rounding in."""
    n = 0x18 + audio_bytes
    return (n + PAGE - 1) // PAGE * PAGE


def seconds(n_bytes):
    return n_bytes / float(BYTES_PER_FRAME * TARGET_RATE)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        epilog="Read-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="first", type=int, default=1,
                    metavar="SAMPLE", help="first sample (default: 1)")
    ap.add_argument("--to", dest="last", type=int, default=panel.SAMPLES,
                    metavar="SAMPLE", help="last sample, inclusive (default: 128)")
    ap.add_argument("--used", action="store_true",
                    help="hide empty samples")
    ap.add_argument("--all", action="store_true",
                    help="print every sample, without collapsing empty runs")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    # Panel numbers in, 0-based slots out, before anything downstream
    # sees them. Inclusive at both ends either way.
    args.first = panel.to_slot(ap, args.first, panel.SAMPLES, "--from sample")
    args.last = panel.to_slot(ap, args.last, panel.SAMPLES, "--to sample")
    if args.last < args.first:
        ap.error("--to must not be below --from")

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    rows = []
    # `slot` and `MCC` side by side was the same hedge preset list made: two
    # numbers one apart, neither declared authoritative. The instrument and MCC
    # agree on 1-128, so that is the column. `slot` stays in --json.
    out = Listing("sample   seconds       bytes  name", "sample", args.last + 1,
                  collapse=not args.all)
    try:
        for slot in range(args.first, args.last + 1):
            name, length, addr = read_slot_record(synth, slot, seqgen)
            r = dict(slot=slot, panel=slot + 1, name=name,
                     bytes=length, address=addr,
                     seconds=round(seconds(length), 3) if length else 0.0)
            rows.append(r)
            if r["bytes"]:
                out.add(r["panel"], "%6d  %8.2f  %10d  %s"
                        % (r["panel"], r["seconds"], r["bytes"], r["name"] or ""))
            elif args.used and r["bytes"] is not None:
                out.reading(r["panel"])
            else:
                label = "(no reply)" if r["bytes"] is None else "(empty)"
                out.add(r["panel"], "%6d   %s" % (r["panel"], label),
                        key=label, label=label)
    finally:
        synth.close()
        out.finish()

    quiet = [r["slot"] for r in rows if r["bytes"] is None]
    used = [r for r in rows if r["bytes"]]

    total = sum(r["bytes"] for r in used)
    print("\n%d of %d samples in use, %.1f s of audio, %s bytes%s"
          % (len(used), len(rows), seconds(total), format(total, ","),
             "; no reply from %d samples" % len(quiet) if quiet else ""))

    # Space, from the records' own addresses rather than from the audio
    # totals -- the difference is the per-record header and page rounding.
    space = {}
    with_addr = [r for r in used if r.get("address")]
    if with_addr and args.first == 0 and args.last == LAST_SLOT:
        last = max(with_addr, key=lambda r: r["address"])
        end = last["address"] + allocated(last["bytes"])
        used_bytes = end - SAMPLE_REGION_BASE
        free_bytes = max(0, SAMPLE_REGION_END - end)
        capacity = SAMPLE_REGION_END - SAMPLE_REGION_BASE
        space = dict(region_base=SAMPLE_REGION_BASE,
                     region_end=SAMPLE_REGION_END,
                     capacity_bytes=capacity,
                     capacity_seconds=round(seconds(capacity), 1),
                     used_bytes=used_bytes,
                     used_seconds=round(seconds(used_bytes), 1),
                     free_bytes=free_bytes,
                     free_seconds=round(seconds(free_bytes), 1),
                     overhead_bytes=used_bytes - total)
        print("flash:  %s of %s bytes used (%.0f%%), %s free"
              % (format(used_bytes, ","), format(capacity, ","),
                 100.0 * used_bytes / capacity, format(free_bytes, ",")))
        print("        %.1f s used, %.1f s free, of about %.1f s"
              % (seconds(used_bytes), seconds(free_bytes), seconds(capacity)))
        print("        %s bytes of that is per-record header and page rounding"
              % format(used_bytes - total, ","))
    elif with_addr:
        print("(run without --from/--to for the used and free totals)")
    print("The audio cannot be read back off the device -- see --help.")

    jsonout.result(first=args.first, last=args.last, slots=len(rows),
                   in_use=len(used), total_bytes=total,
                   total_seconds=round(seconds(total), 3),
                   no_reply=quiet, space=space or None, samples=rows)
    return 1 if quiet else 0


if __name__ == "__main__":
    sys.exit(main())
