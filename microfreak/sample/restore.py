#!/usr/bin/env python3
"""Write an MCC sample bank to the instrument.

    mfreak sample restore Factory.mfsbz
    mfreak sample restore mine.mfsbz --from 53 --to 60 --yes
    mfreak sample restore mine.mfsbz --empty-unused

The source is a .mfsbz, such as MCC's sample export or the factory bank
from `asset import`. Each sample goes to its own number with its name.
Other samples on the instrument are left alone, unless --empty-unused
deletes the ones the file has empty.

This replaces every sample it writes, and sample audio cannot be read back
off the instrument, so the old ones are gone unless you have their files.
It lists what it would change and asks first. --yes skips the question.
Each sample's name is read back and checked unless --no-verify.
"""
import argparse
import os
import sys

import microfreak.bank as B
from microfreak import confirm
from microfreak import jsonout
from microfreak import mccfile
from microfreak import panel
from microfreak.midi import checksum_free_seq, open_synth
from microfreak.sample.erase import erase_sample
from microfreak.sample.listing import (SAMPLE_REGION_END, allocated, seconds)
from microfreak.sample.write import (decoded_len, read_slot_meta, read_slot_record,
                                     write_sample)


def load_source(path):
    """-> ([(slot, name, pcm)], [slots the file has empty]), 0-based."""
    try:
        mccfile.check_ext(path, "sample", bank=True)
    except mccfile.FileError as exc:
        raise SystemExit(str(exc))
    if not os.path.isfile(path):
        raise SystemExit("%s: no such file" % path)
    try:
        items = B.read_bank(path)
    except (ValueError, KeyError) as exc:
        raise SystemExit("%s: not a readable MCC file. %s" % (path, exc))
    out, empty = [], []
    for it in items:
        if not 1 <= it.slot <= panel.SAMPLES:
            raise SystemExit("%s: slot %d is past sample %d"
                             % (path, it.slot, panel.SAMPLES))
        if it.is_empty():
            empty.append(it.slot - 1)
            continue
        if len(it.values) < 2 or len(it.values) % 2:
            raise SystemExit("%s: slot %d is not 16-bit audio" % (path, it.slot))
        name = it.name[:12]
        out.append((it.slot - 1, name, it.pcm()))
    return out, empty


def free_bytes(synth, seqgen):
    """-> sample flash free past the last record, as `sample list` counts it."""
    end = None
    for slot in range(panel.SAMPLES):
        _, length, addr = read_slot_record(synth, slot, seqgen)
        if length and addr:
            stop = addr + allocated(length)
            end = stop if end is None else max(end, stop)
    return SAMPLE_REGION_END - end if end is not None else None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", metavar="FILE.mfsbz",
                    help="an MCC sample bank (.mfsbz)")
    ap.add_argument("--from", dest="first", type=int, default=1,
                    metavar="SAMPLE", help="first sample to restore (default: 1)")
    ap.add_argument("--to", dest="last", type=int, default=panel.SAMPLES,
                    metavar="SAMPLE",
                    help="last sample to restore, inclusive (default: 128)")
    ap.add_argument("--empty-unused", action="store_true",
                    help="also delete the samples the file has empty")
    ap.add_argument("--yes", action="store_true", help="write without asking")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip checking each name afterwards")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    first = panel.to_slot(ap, args.first, panel.SAMPLES, "--from sample")
    last = panel.to_slot(ap, args.last, panel.SAMPLES, "--to sample")
    if last < first:
        ap.error("--to must not be below --from")

    samples, empty = load_source(args.source)
    samples = [s for s in samples if first <= s[0] <= last]
    empty = [s for s in empty if first <= s <= last] if args.empty_unused else []
    if not samples and not empty:
        sys.exit("%s has no samples in %d-%d" % (args.source, args.first, args.last))
    wires = {slot: mccfile.sample_wire(pcm) for slot, _, pcm in samples}

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    written, erased, failed = [], [], []
    interrupted = False
    try:
        current = {s: read_slot_meta(synth, s, seqgen)
                   for s in sorted(set(wires) | set(empty))}
        to_empty = [s for s in empty if current[s][1]]
        rows = sorted([(slot, name) for slot, name, _ in samples]
                      + [(s, "(empty)") for s in to_empty])
        for slot, name in rows:
            old = current[slot][0] if current[slot][1] else "(empty)"
            print("  %3d  %-12s <- %s" % (slot + 1, old, name))
        if not rows:
            print("nothing to change")
            return 0

        need = sum(allocated(decoded_len(w)) for w in wires.values())
        free = free_bytes(synth, seqgen)
        if free is not None:
            print("needs about %.1f s of sample memory; %.1f s is free"
                  % (seconds(need), seconds(free)))
            if need > free:
                print("warning: that may not fit. Delete samples first with "
                      "`mfreak sample erase`.", file=sys.stderr)

        question = " and ".join(part for part in (
            "write %d sample%s" % (len(samples), "" if len(samples) == 1 else "s")
            if samples else "",
            "delete %d" % len(to_empty) if to_empty else "") if part)
        if not confirm.ask("%s from %s?" % (question[0].upper() + question[1:],
                                            os.path.basename(args.source)),
                           args.yes):
            jsonout.result(source=args.source, slots=[s + 1 for s in wires],
                           empty=[s + 1 for s in to_empty], written=[],
                           erased=[], failed=[], dry_run=True)
            return 0

        try:
            for slot in to_empty:
                try:
                    erase_sample(synth, slot, seqgen)
                    _, length = read_slot_meta(synth, slot, seqgen)
                except (TimeoutError, RuntimeError) as exc:
                    failed.append(dict(panel=slot + 1, error=str(exc)))
                    print("  %3d  FAILED: %s" % (slot + 1, exc), file=sys.stderr)
                    continue
                if length:
                    failed.append(dict(panel=slot + 1, error="still holds a sample"))
                    print("  %3d  FAILED: still holds a sample" % (slot + 1),
                          file=sys.stderr)
                    continue
                erased.append(slot + 1)
                print("  deleted sample %3d" % (slot + 1), file=sys.stderr)
            for i, (slot, name, _) in enumerate(samples):
                try:
                    write_sample(synth, slot, name, wires[slot], seqgen)
                    ok = True
                    if not args.no_verify:
                        got, length = read_slot_meta(synth, slot, seqgen)
                        ok = got == name and length == decoded_len(wires[slot])
                except (TimeoutError, RuntimeError) as exc:
                    failed.append(dict(panel=slot + 1, error=str(exc)))
                    print("  %3d  FAILED: %s" % (slot + 1, exc), file=sys.stderr)
                    continue
                if not ok:
                    failed.append(dict(panel=slot + 1, error="read back differs"))
                    print("  %3d  FAILED: read back differs" % (slot + 1),
                          file=sys.stderr)
                    continue
                written.append(slot + 1)
                print("  %3d/%d  sample %3d  %s"
                      % (i + 1, len(samples), slot + 1, name), file=sys.stderr)
        except KeyboardInterrupt:
            interrupted = True
            print("\nstopped", file=sys.stderr)
    finally:
        synth.close()

    if samples:
        print("restored %d of %d samples%s"
              % (len(written), len(samples),
                 "" if args.no_verify else ", each name read back"))
    if to_empty:
        print("deleted %d of %d" % (len(erased), len(to_empty)))
    if failed:
        print("error: %d failed: %s"
              % (len(failed), ", ".join(str(f["panel"]) for f in failed)),
              file=sys.stderr)
    jsonout.result(source=args.source, slots=[s + 1 for s in wires],
                   written=written, erased=erased, failed=failed,
                   dry_run=False, interrupted=interrupted)
    if interrupted:
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
