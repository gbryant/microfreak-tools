#!/usr/bin/env python3
"""Save the instrument's presets as an MCC project. Takes about 50 s.

    mfreak preset backup mine.mfprojz
    mfreak preset backup pads.mfprojz --from 1 --to 64

The file is an MCC project (.mfprojz), named after the file. Each preset
keeps its number, name and category. Presets outside --from and --to are
left out, and the file is read back and checked after it is written.
Empty slots are saved as empty, as MCC saves them. A backup that starts
after preset 1 also has empty entries before --from, which MCC needs to put
each preset in its own slot. Restore it with the same --from and --to.
Restore it with `preset restore mine.mfprojz`.
"""
import argparse
import os
import random
import sys

import microfreak.bank as B
from microfreak import jsonout
from microfreak import mccfile
from microfreak import panel
from microfreak.midi import checksum_free_seq, open_synth
from microfreak.preset.read import category_of, read_name_record, read_preset, schema_of


def read_slots(synth, seqgen, slots, label="backup"):
    """Read `slots` off the instrument.

    -> ({slot: (name, category, payload, schema)}, [{panel, error}]), with
    name and payload None for an empty slot.
    """
    got, failed = {}, []
    for i, slot in enumerate(slots):
        try:
            # The category lives in the name record, not in the payload.
            record = read_name_record(synth, slot, seqgen)
            if schema_of(record) is None:
                got[slot] = (None, 0, None, None)
                continue
            name, payload = read_preset(synth, slot, seqgen)
        except (TimeoutError, RuntimeError) as exc:
            failed.append(dict(panel=slot + 1, error=str(exc)))
            print("preset %d FAILED: %s" % (slot + 1, exc), file=sys.stderr)
            continue
        got[slot] = (name, category_of(record), payload, schema_of(record))
        if i % 32 == 0 or i == len(slots) - 1:
            print("  %s %4d/%d  preset %3d  %s"
                  % (label, i + 1, len(slots), slot + 1, name), file=sys.stderr)
    return got, failed


def save_project(path, presets, pad=True):
    """Write {slot: (name, category, payload, schema)} as a .mfprojz and read
    it back. `pad` fills the slots before the last with empty entries, which
    MCC needs to place each preset in its own slot.

    -> the presets that did not read back identically, by panel number.
    """
    project = os.path.splitext(os.path.basename(path))[0] or "Backup"
    items = [(slot + 1, mccfile.empty_archive("%s-A%d" % (project, slot + 1))
              if p is None else mccfile.preset_archive(n, c, p, s))
             for slot, (n, c, p, s) in sorted(presets.items())]
    B.write_bank(path, items, top=project, subdir="01-%s-A" % project,
                 ext="mbp", pad=pad)
    back = {it.slot: it for it in B.read_bank(path)}
    return [slot + 1 for slot, (n, c, p, s) in presets.items()
            if slot + 1 not in back
            or (not back[slot + 1].is_empty() if p is None
                else back[slot + 1].name != n or back[slot + 1].wire() != p)]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]) + "\nRead-only.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", metavar="FILE.mfprojz", help="the project to write")
    ap.add_argument("--from", dest="first", type=int, default=1,
                    metavar="PRESET", help="first preset (default: 1)")
    ap.add_argument("--to", dest="last", type=int, default=panel.PRESETS,
                    metavar="PRESET", help="last preset, inclusive (default: 512)")
    ap.add_argument("--verify", type=int, default=8, metavar="N",
                    help="re-read N random presets afterwards and compare; "
                         "0 skips it (default: 8)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite the file if it exists")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    first = panel.to_slot(ap, args.first, panel.PRESETS, "--from preset")
    last = panel.to_slot(ap, args.last, panel.PRESETS, "--to preset")
    if last < first:
        ap.error("--to must not be below --from")
    if os.path.splitext(args.out)[1].lower() != ".mfprojz":
        ap.error("the backup is an MCC project: name it FILE.mfprojz")
    if os.path.exists(args.out) and not args.force:
        sys.exit("%s already exists. --force overwrites it." % args.out)

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        presets, failed = read_slots(synth, seqgen, list(range(first, last + 1)))

        # A transport that returned one slot's data for every slot asked for
        # is invisible at the time; re-reading a few catches it.
        checked, mismatched = 0, []
        if args.verify and presets:
            used = sorted(s for s, v in presets.items() if v[2] is not None)
            for slot in random.sample(used, min(args.verify, len(used))):
                _, payload = read_preset(synth, slot, seqgen)
                checked += 1
                if payload != presets[slot][2]:
                    mismatched.append(slot + 1)
    finally:
        synth.close()

    if not presets:
        sys.exit("no presets were read, so nothing was written")
    unreadable = save_project(args.out, presets)

    empty = sum(1 for v in presets.values() if v[2] is None)
    print("\n%d presets -> %s" % (len(presets) - empty, args.out))
    if empty:
        print("%d empty slots saved as empty" % empty)
    if checked:
        print("verify: %d re-read, %s"
              % (checked, "all identical" if not mismatched
                 else "MISMATCH at %s" % mismatched))
    if unreadable:
        print("error: %d presets did not read back from the file: %s"
              % (len(unreadable), unreadable[:8]), file=sys.stderr)
    if failed:
        print("error: %d presets could not be read: %s"
              % (len(failed), ", ".join(str(f["panel"]) for f in failed)),
              file=sys.stderr)
    jsonout.result(out=os.path.abspath(args.out), count=len(presets) - empty,
                   empty=empty,
                   failed=failed, verified=checked,
                   mismatched=mismatched, unreadable=unreadable)
    return 1 if (failed or mismatched or unreadable) else 0


if __name__ == "__main__":
    sys.exit(main())
