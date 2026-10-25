#!/usr/bin/env python3
"""Empty samples on the instrument, as MCC's delete does.

    mfreak sample erase 60
    mfreak sample erase --from 53 --to 60

Samples that are already empty are left alone.

This deletes the samples it empties, and sample audio cannot be read back
off the instrument, so keep the files you uploaded. It lists them and asks
first. --yes skips the question.
"""
import argparse
import sys

from microfreak import confirm
from microfreak import jsonout
from microfreak import panel
from microfreak.midi import checksum_free_seq, open_synth
from microfreak.sample.write import build_name_chunk, read_slot_meta, send_name_again


def erase_sample(synth, slot, seqgen):
    """Empty a slot the way MCC deletes a sample: one name chunk declaring no
    audio, sent with 03 5A alone (captures/delete-sample.pcapng)."""
    send_name_again(synth, slot, build_name_chunk("", audio_len=0, slot=slot),
                    seqgen)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sample", type=int, nargs="?", metavar="SAMPLE",
                    help="the sample to empty (1-128)")
    ap.add_argument("--from", dest="first", type=int, metavar="SAMPLE",
                    help="first sample to empty")
    ap.add_argument("--to", dest="last", type=int, metavar="SAMPLE",
                    help="last sample to empty, inclusive")
    ap.add_argument("--yes", action="store_true",
                    help="empty them without asking")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    ranged = args.first is not None or args.last is not None
    if (args.sample is not None) + ranged != 1:
        ap.error("give SAMPLE, or --from and --to")
    if args.sample is not None:
        first = last = panel.to_slot(ap, args.sample, panel.SAMPLES, "sample")
    else:
        if args.first is None or args.last is None:
            ap.error("--from and --to go together")
        first = panel.to_slot(ap, args.first, panel.SAMPLES, "--from sample")
        last = panel.to_slot(ap, args.last, panel.SAMPLES, "--to sample")
        if last < first:
            ap.error("--to must not be below --from")

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    erased, failed = [], []
    interrupted = False
    try:
        targets = []
        for slot in range(first, last + 1):
            name, length = read_slot_meta(synth, slot, seqgen)
            if length:
                targets.append((slot, name))
        for slot, name in targets:
            print("  %3d  %s" % (slot + 1, name))
        if not targets:
            print("already empty")
            jsonout.result(empty=[], erased=[], failed=[], dry_run=False)
            return 0
        if not confirm.ask("Delete %d sample%s?" % (
                len(targets), "" if len(targets) == 1 else "s"), args.yes):
            jsonout.result(empty=[s + 1 for s, _ in targets], erased=[],
                           failed=[], dry_run=True)
            return 0

        try:
            for slot, _ in targets:
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
                print("  emptied sample %3d" % (slot + 1), file=sys.stderr)
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
