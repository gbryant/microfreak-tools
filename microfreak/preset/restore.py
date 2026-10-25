#!/usr/bin/env python3
"""Write an MCC project or bank back to the instrument.

    mfreak preset restore mine.mfprojz
    mfreak preset restore Factory6.0.mfprojz --from 1 --to 64 --yes
    mfreak preset restore mine.mfprojz --empty-unused --show-current

The source is a .mfprojz or .mfbz, such as one from `preset backup`. Each
preset goes to its own number with its name and category. Other presets on
the instrument are left alone, unless --empty-unused empties the ones the
file has empty. Presets from older firmware are converted for the
instrument's firmware unless --no-convert.

This overwrites every preset it writes, up to all 512, and ignores the
instrument's Mem protect setting. It lists what it would write and asks
first. --yes skips the question. --show-current also lists the names being
replaced. --backup FILE.mfprojz first saves the presets about to be replaced.

Each preset takes a few seconds, and is read back and checked unless
--no-verify.
"""
import argparse
import os
import sys

import microfreak.bank as B
from microfreak import confirm
from microfreak import jsonout
from microfreak import mccfile
from microfreak.preset import convert
from microfreak.preset import tlv as T
from microfreak import panel
from microfreak.midi import checksum_free_seq, open_synth
from microfreak.preset.backup import read_slots, save_project
from microfreak.preset.read import name_of, read_name_record, read_preset
from microfreak.preset.write import (DATA_LEN, NAME_MAX, build_name_record,
                                     erase_preset, slot_is_empty, write_preset)

CATEGORIES = 12


def load_source(path):
    """-> ([Item] for the presets in a .mfprojz or .mfbz, [the 0-based slots
    it has empty])."""
    try:
        kind, bank = mccfile.kind_of(path)
        if kind != "preset" or not bank:
            raise mccfile.FileError("%s: restore takes a preset project or "
                                    "bank (.mfprojz, .mfbz). For one preset, "
                                    "use `preset write N --file`." % path)
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
        if it.is_empty():
            empty.append(it.slot - 1)
            continue
        if len(it.values) != B.PRESET_WIRE_LEN:
            raise SystemExit("%s: slot %d is not a preset" % (path, it.slot))
        if not any(True for _ in T.fields(T.decode_7to8(list(it.wire())))):
            empty.append(it.slot - 1)   # an empty slot's filler, no parameters
            continue
        out.append(it)
    return out, empty


def as_rows(items):
    return sorted((it.slot - 1,) + mccfile.preset_of(it) for it in items)


def check(presets):
    """Refuse anything the instrument would reject, before a port opens."""
    for slot, name, category, payload in presets:
        where = "preset %d (%r)" % (slot + 1, name)
        if not 0 <= slot < panel.PRESETS:
            raise SystemExit("%s: no such preset number" % where)
        if len(payload) != DATA_LEN:
            raise SystemExit("%s: %d bytes, not %d"
                             % (where, len(payload), DATA_LEN))
        if len(name.encode("ascii", "replace")) > NAME_MAX:
            raise SystemExit("%s: the name is longer than %d characters"
                             % (where, NAME_MAX))
        if not 0 <= category < CATEGORIES:
            raise SystemExit("%s: category %d is not 0-%d"
                             % (where, category, CATEGORIES - 1))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", metavar="FILE.mfprojz",
                    help="an MCC preset project or bank (.mfprojz, .mfbz)")
    ap.add_argument("--from", dest="first", type=int, default=1,
                    metavar="PRESET", help="first preset to restore (default: 1)")
    ap.add_argument("--to", dest="last", type=int, default=panel.PRESETS,
                    metavar="PRESET",
                    help="last preset to restore, inclusive (default: 512)")
    ap.add_argument("--empty-unused", action="store_true",
                    help="also empty the presets the file has empty")
    ap.add_argument("--show-current", action="store_true",
                    help="list the names being replaced before asking")
    ap.add_argument("--backup", metavar="FILE.mfprojz",
                    help="first save the presets about to be replaced here, "
                         "to undo with restore")
    ap.add_argument("--yes", action="store_true",
                    help="write without asking")
    ap.add_argument("--no-convert", action="store_true",
                    help="send presets as they are stored, instead of "
                         "converting them for the instrument's firmware")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip reading each preset back")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    first = panel.to_slot(ap, args.first, panel.PRESETS, "--from preset")
    last = panel.to_slot(ap, args.last, panel.PRESETS, "--to preset")
    if last < first:
        ap.error("--to must not be below --from")
    if args.backup:
        if os.path.splitext(args.backup)[1].lower() != ".mfprojz":
            ap.error("--backup is an MCC project: name it FILE.mfprojz")
        if os.path.exists(args.backup):
            sys.exit("%s already exists. Pick a new file for --backup."
                     % args.backup)

    items, empty = load_source(args.source)
    items = [it for it in items if first <= it.slot - 1 <= last]
    empty = [s for s in empty if first <= s <= last] if args.empty_unused else []
    presets = as_rows(items)
    if not presets and not empty:
        sys.exit("%s has no presets in %d-%d"
                 % (args.source, args.first, args.last))
    check(presets)
    slots = [p[0] for p in presets]

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        convert.check_protocol(synth)
    except convert.ConvertError as exc:
        synth.close()
        sys.exit(str(exc))
    if not args.no_convert:
        try:
            items, notes = convert.to_send(synth, items)
        except convert.ConvertError as exc:
            synth.close()
            sys.exit("%s: %s" % (args.source, exc))
        converted = [n for n in notes if n.startswith("converted")]
        for note in notes:
            if not note.startswith("converted"):
                print(note, file=sys.stderr)
        if converted:
            print("%d presets from older firmware converted for this "
                  "instrument" % len(converted), file=sys.stderr)
        presets = as_rows(items)
        check(presets)
    written, erased, failed = [], [], []
    interrupted = False
    try:
        # Only slots that hold a preset now need emptying.
        records = {s: read_name_record(synth, s, seqgen) for s in empty}
        to_empty = [s for s in empty if not slot_is_empty(records[s])]
        current = {s: name_of(records[s]) for s in to_empty}
        if args.show_current:
            current.update((s, name_of(read_name_record(synth, s, seqgen)))
                           for s in slots)
        rows = sorted([(slot, name) for slot, name, _, _ in presets]
                      + [(s, "(empty)") for s in to_empty])
        for slot, name in rows:
            if slot in current:
                print("  %3d  %-23s <- %s" % (slot + 1, current[slot], name))
            else:
                print("  %3d  %s" % (slot + 1, name))
        if not rows:
            print("nothing to change")
            return 0
        question = " and ".join(part for part in (
            "write %d preset%s" % (len(presets), "" if len(presets) == 1 else "s")
            if presets else "",
            "empty %d" % len(to_empty) if to_empty else "") if part)
        if not confirm.ask("%s from %s?" % (question[0].upper() + question[1:],
                                            os.path.basename(args.source)),
                           args.yes):
            jsonout.result(source=args.source, presets=len(presets),
                           slots=[s + 1 for s in slots],
                           empty=[s + 1 for s in to_empty], written=[],
                           erased=[], failed=[], dry_run=True)
            return 0

        if args.backup:
            saved, bad = read_slots(synth, seqgen, sorted(slots + to_empty))
            if bad or not saved:
                sys.exit("the backup failed at preset %s, so nothing was "
                         "written" % (bad[0]["panel"] if bad else slots[0] + 1))
            if save_project(args.backup, saved, pad=False):
                sys.exit("the backup did not read back, so nothing was written")
            print("backed up %d presets -> %s" % (len(slots), args.backup))

        try:
            schemas = {it.slot - 1: it.archive.schema for it in items}
            for i, (slot, name, category, payload) in enumerate(presets):
                record = build_name_record(slot, name, payload, category,
                                           schemas.get(slot))
                try:
                    write_preset(synth, slot, record, payload, seqgen)
                    ok = True
                    if not args.no_verify:
                        got_name, got = read_preset(synth, slot, seqgen)
                        ok = got_name == name_of(record) and got == payload
                except (TimeoutError, RuntimeError) as exc:
                    failed.append(dict(panel=slot + 1, error=str(exc)))
                    print("  %3d  FAILED: %s" % (slot + 1, exc), file=sys.stderr)
                    continue
                if not ok:
                    failed.append(dict(panel=slot + 1,
                                       error="read back differs"))
                    print("  %3d  FAILED: read back differs" % (slot + 1),
                          file=sys.stderr)
                    continue
                written.append(slot + 1)
                print("  %4d/%d  preset %3d  %s"
                      % (i + 1, len(presets), slot + 1, name), file=sys.stderr)
            for slot in to_empty:
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

    if presets:
        print("restored %d of %d presets%s"
              % (len(written), len(presets),
                 "" if args.no_verify else ", each read back and checked"))
    if to_empty:
        print("emptied %d of %d" % (len(erased), len(to_empty)))
    if failed:
        print("error: %d failed: %s"
              % (len(failed), ", ".join(str(f["panel"]) for f in failed)),
              file=sys.stderr)
    if (interrupted or failed) and args.backup:
        print("to undo: mfreak preset restore %s --empty-unused --yes"
              % args.backup, file=sys.stderr)
    jsonout.result(source=args.source, presets=len(presets),
                   slots=[s + 1 for s in slots], written=written,
                   erased=erased, failed=failed, backup=args.backup, dry_run=False,
                   interrupted=interrupted)
    if interrupted:
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
