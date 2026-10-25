#!/usr/bin/env python3
"""Write an MCC wavetable bank back to the instrument.

    mfreak wavetable restore mine.mfwbz
    mfreak wavetable restore Factory.mfwbz --from 1 --to 4 --yes
    mfreak wavetable restore Factory.mfwbz --backup before.mfwbz
    mfreak wavetable restore mine.mfwbz --empty-unused

The source is a .mfwbz, such as one from `wavetable backup`. Each wavetable
goes to its own number with its name. Wavetables the file has empty are
left alone on the instrument, unless --empty-unused deletes them.

This overwrites every wavetable it writes. It lists them and asks first.
--yes skips the question. --backup FILE.mfwbz first saves the wavetables
about to be replaced. Each one is read back and checked unless --no-verify.
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
from microfreak.wavetable.backup import read_slots, save_bank
from microfreak.wavetable.erase import erase_wavetable
from microfreak.wavetable.listing import read_name
from microfreak.wavetable.read import read_wavetable
from microfreak.wavetable.write import build_name_chunk, write_wavetable


def load_source(path):
    """-> ([(slot, name, pcm)], [slots the file has empty]), 0-based."""
    try:
        mccfile.check_ext(path, "wavetable", bank=True)
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
        if not 1 <= it.slot <= panel.WAVETABLES:
            raise SystemExit("%s: slot %d is past wavetable %d"
                             % (path, it.slot, panel.WAVETABLES))
        if it.is_empty():
            empty.append(it.slot - 1)
            continue
        if len(it.values) != mccfile.WAVETABLE_BYTES:
            raise SystemExit("%s: slot %d is not a wavetable" % (path, it.slot))
        try:
            mccfile.check_name(it.name, "wavetable")
        except mccfile.FileError as exc:
            raise SystemExit("%s: slot %d: %s" % (path, it.slot, exc))
        out.append((it.slot - 1, it.name, it.pcm()))
    return out, empty


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", metavar="FILE.mfwbz",
                    help="an MCC wavetable bank (.mfwbz)")
    ap.add_argument("--from", dest="first", type=int, default=1,
                    metavar="WAVETABLE",
                    help="first wavetable to restore (default: 1)")
    ap.add_argument("--to", dest="last", type=int, default=panel.WAVETABLES,
                    metavar="WAVETABLE",
                    help="last wavetable to restore, inclusive (default: 16)")
    ap.add_argument("--empty-unused", action="store_true",
                    help="also delete the wavetables the file has empty")
    ap.add_argument("--backup", metavar="FILE.mfwbz",
                    help="first save the wavetables about to be replaced here, "
                         "to undo with restore")
    ap.add_argument("--yes", action="store_true", help="write without asking")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip reading each wavetable back")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0,
                    help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    first = panel.to_slot(ap, args.first, panel.WAVETABLES, "--from wavetable")
    last = panel.to_slot(ap, args.last, panel.WAVETABLES, "--to wavetable")
    if last < first:
        ap.error("--to must not be below --from")
    if args.backup:
        try:
            mccfile.check_ext(args.backup, "wavetable", bank=True)
        except mccfile.FileError as exc:
            ap.error(str(exc))
        if os.path.exists(args.backup):
            sys.exit("%s already exists. Pick a new file for --backup."
                     % args.backup)

    tables, empty = load_source(args.source)
    tables = [t for t in tables if first <= t[0] <= last]
    empty = [s for s in empty if first <= s <= last] if args.empty_unused else []
    if not tables and not empty:
        sys.exit("%s has no wavetables in %d-%d"
                 % (args.source, args.first, args.last))

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    written, erased, failed = [], [], []
    interrupted = False
    try:
        current = {s: read_name(synth, s, seqgen)
                   for s in sorted({t[0] for t in tables} | set(empty))}
        to_empty = [s for s in empty if current[s] is not None]
        rows = sorted([(slot, name) for slot, name, _ in tables]
                      + [(s, "(empty)") for s in to_empty])
        for slot, name in rows:
            print("  %2d  %-16s <- %s" % (slot + 1, current[slot] or "(empty)", name))
        if not rows:
            print("nothing to change")
            return 0
        question = " and ".join(part for part in (
            "write %d wavetable%s" % (len(tables), "" if len(tables) == 1 else "s")
            if tables else "",
            "delete %d" % len(to_empty) if to_empty else "") if part)
        if not confirm.ask("%s from %s?" % (question[0].upper() + question[1:],
                                            os.path.basename(args.source)),
                           args.yes):
            jsonout.result(source=args.source, slots=[t[0] + 1 for t in tables],
                           empty=[s + 1 for s in to_empty], written=[],
                           erased=[], failed=[], dry_run=True)
            return 0

        if args.backup:
            saved, bad = read_slots(synth, seqgen,
                                    sorted({t[0] for t in tables} | set(to_empty)))
            if bad or not saved:
                sys.exit("the backup failed, so nothing was written")
            if save_bank(args.backup, saved):
                sys.exit("the backup did not read back, so nothing was written")
            print("backed up %d wavetable%s -> %s" % (len(saved), "" if len(saved) == 1 else "s", args.backup))

        try:
            for slot in to_empty:
                try:
                    erase_wavetable(synth, slot, seqgen)
                    gone = read_name(synth, slot, seqgen) is None
                except (TimeoutError, RuntimeError) as exc:
                    failed.append(dict(panel=slot + 1, error=str(exc)))
                    print("  %2d  FAILED: %s" % (slot + 1, exc), file=sys.stderr)
                    continue
                if not gone:
                    failed.append(dict(panel=slot + 1, error="still holds a wavetable"))
                    print("  %2d  FAILED: still holds a wavetable" % (slot + 1),
                          file=sys.stderr)
                    continue
                erased.append(slot + 1)
                print("  deleted wavetable %2d" % (slot + 1), file=sys.stderr)
            for slot, name, pcm in tables:
                frames = mccfile.wavetable_frames(pcm)
                try:
                    write_wavetable(synth, slot, build_name_chunk(slot, name),
                                    frames, seqgen)
                    ok = True
                    if not args.no_verify:
                        got_name, got = read_wavetable(synth, slot, seqgen)
                        ok = got_name == name and got == frames
                except (TimeoutError, RuntimeError) as exc:
                    failed.append(dict(panel=slot + 1, error=str(exc)))
                    print("  %2d  FAILED: %s" % (slot + 1, exc), file=sys.stderr)
                    continue
                if not ok:
                    failed.append(dict(panel=slot + 1, error="read back differs"))
                    print("  %2d  FAILED: read back differs" % (slot + 1),
                          file=sys.stderr)
                    continue
                written.append(slot + 1)
                print("  wrote wavetable %2d  %s" % (slot + 1, name),
                      file=sys.stderr)
        except KeyboardInterrupt:
            interrupted = True
            print("\nstopped", file=sys.stderr)
    finally:
        synth.close()

    if tables:
        print("restored %d of %d wavetables%s"
              % (len(written), len(tables),
                 "" if args.no_verify else ", each read back and checked"))
    if to_empty:
        print("deleted %d of %d" % (len(erased), len(to_empty)))
    if failed:
        print("error: %d failed: %s"
              % (len(failed), ", ".join(str(f["panel"]) for f in failed)),
              file=sys.stderr)
    if (interrupted or failed) and args.backup:
        print("to undo: mfreak wavetable restore %s --yes" % args.backup,
              file=sys.stderr)
    jsonout.result(source=args.source, slots=[t[0] + 1 for t in tables],
                   written=written, erased=erased, failed=failed,
                   backup=args.backup,
                   dry_run=False, interrupted=interrupted)
    if interrupted:
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
