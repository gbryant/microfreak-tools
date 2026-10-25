#!/usr/bin/env python3
"""Copy Arturia's factory presets, samples and wavetables from MCC's files.

    mfreak asset import --list                     show what is installed
    mfreak asset import --outdir ~/mf-assets       write the export files
    mfreak asset import --outdir DATA --by-kind    DATA/presets, samples, wavetables

Needs MIDI Control Center installed. This is the only source of factory
samples, and the only way back to factory presets once you have edited the
instrument. Nothing is shipped with this tool; the files come from your own
MCC install.

For microfreak-emu, use `--outdir ../microfreak-emu/data --by-kind`.
"""

# Where the factory content is (MCC installs to /, so the path is the same on
# every Mac). Only these subtrees are factory content:
#
#   /Library/Arturia/MIDI Control Center/Templates/MicroFreak/
#     Local/Factory/Factory6.0/01-factory_6-A/      512 .mbp  (384 + 128 empty)
#     Local/Factory/New Presets 5.0/...             512 .mbp
#     Local/Factory/New Presets 4.0/...             384 .mbp
#     Local/Factory/Factory3.0/...                  384 .mbp
#     Samples/Local/Factory/Factory/01-Factory-A/   128 .mfs  (52 + 76 empty)
#     Wavetables/Local/Factory/Factory/01-Factory-A/ 16 .mfw
#
# Device/Reserved/ mirrors the user's instrument and Wavetables/InitWavetable/
# is a blank template; neither is imported. See FACTORY_SUBTREES.
#
# Evidence: all 384 non-empty Factory6.0 presets match an MCC export made by
# hand, byte for byte. MCC's Factory wavetables equal image 4 of the
# 5.0.0.2084 .mff, audio and names, and microfreak-emu builds an identical
# flash image from either.
#
# Exports are discovered by content (any folder holding .mbp, .mfs or .mfw
# members), not by a fixed folder list, because MCC's layout changes.

import argparse
import glob
import os
import re
import sys

import microfreak.bank as bank
from microfreak import jsonout

# install-location="/" in MIDI Control Center-*-templates.pkg, so this is an
# absolute system path and not under any user's home.
MCC_ROOT = "/Library/Arturia/MIDI Control Center/Templates/MicroFreak"

# member extension -> (what it is, the bank container to write)
KINDS = {
    "mbp": ("presets", ".mfprojz"),
    "mfs": ("samples", ".mfsbz"),
    "mfw": ("wavetables", ".mfwbz"),
}


# The three subtrees that hold FACTORY content, and nothing else. MCC's tree
# has two other kinds of thing in it that are not factory banks and must not be
# imported as if they were:
#
#   Device/Reserved/            MCC's mirror of whatever is on YOUR instrument.
#                               Empty for presets and samples on a fresh
#                               install, but the wavetable one came back with
#                               16 used -- so a content-only walk imported the
#                               device's tables as "Reserved.mfwbz", which is
#                               not factory content and is already obtainable
#                               with `wavetable read`.
#   Wavetables/InitWavetable/   one unnamed 32-frame table, the blank canvas
#                               for a NEW user wavetable. Not a bank: no slot
#                               numbers, so `_slots()` rejected it and the
#                               import printed a SKIP line about a file that
#                               was never meant to be there.
FACTORY_SUBTREES = (
    os.path.join("Local", "Factory"),
    os.path.join("Samples", "Local", "Factory"),
    os.path.join("Wavetables", "Local", "Factory"),
)


def find_assets(root):
    """-> [(kind, dirpath, [member paths])], one entry per bank found.

    Searches the three FACTORY subtrees, not the whole tree. Within each, a
    leaf directory holding members of one kind is one bank -- found by content
    rather than by name, because `Local/Factory/New Presets 5.0` is MCC's and
    is versioned.

    If none of the three is present, `root` itself is walked: that is what
    makes `--mcc` usable against a single folder someone has copied out, or
    against `Device/Reserved` on purpose.
    """
    roots = [os.path.join(root, sub) for sub in FACTORY_SUBTREES
             if os.path.isdir(os.path.join(root, sub))]
    if not roots:
        roots = [root]
    out = []
    for base in roots:
        for dirpath, _dirs, _names in os.walk(base):
            for ext in sorted(KINDS):
                members = sorted(glob.glob(os.path.join(dirpath, "*.%s" % ext)))
                if members:
                    out.append((ext, dirpath, members))
    return sorted(out, key=lambda e: (e[0], e[1]))


def bank_label(root, dirpath):
    """-> a filename stem for the bank in `dirpath`.

    The leaf is MCC's `NN-<project>-A` holder, which is an artefact; its PARENT
    is the name a person would recognise -- `Factory6.0`, `New Presets 5.0`,
    `Reserved`. Falls back to the leaf if there is no parent inside the root.
    """
    rel = os.path.relpath(dirpath, root)
    parts = [p for p in rel.split(os.sep) if p not in (".", "")]
    label = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else "mcc")
    return re.sub(r"[^A-Za-z0-9._ -]", "_", label).strip() or "mcc"


def read_members(members):
    """-> ([(slot, Archive)], used, empty). Empty placeholders are KEPT.

    MCC writes a full bank: `Factory6.0` is 512 members of which 128 are empty
    slots, one of them literally named `test_empty`. They are kept so the
    import is the same shape as the export, and because `bank._slots()` pins
    MCC's 47 dash-less names by elimination over a FULL bank -- dropping the
    empties would break the arithmetic that makes the rest readable.
    """
    slots = bank._slots(members)
    items, used, empty = [], 0, 0
    for path, slot in sorted(slots.items(), key=lambda kv: kv[1]):
        item = bank.read_item(path)
        items.append((slot, item.archive))
        if item.is_empty():
            empty += 1
        else:
            used += 1
    return items, used, empty


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mcc", default=MCC_ROOT, metavar="DIR",
                    help="where MIDI Control Center's MicroFreak templates "
                         "are (default: %(default)s)")
    ap.add_argument("--outdir", metavar="DIR",
                    help="where to write the export files (required unless "
                         "--list)")
    ap.add_argument("--list", action="store_true",
                    help="show what is installed and write nothing")
    ap.add_argument("--only", choices=sorted({v[0] for v in KINDS.values()}),
                    help="import just one kind")
    ap.add_argument("--by-kind", action="store_true",
                    help="put each kind in its own subfolder of --outdir")
    args = ap.parse_args()

    if not os.path.isdir(args.mcc):
        sys.exit(
            "no MIDI Control Center templates at\n  %s\n\n"
            "If MCC is installed elsewhere, point --mcc at the MicroFreak "
            "folder\ninside its Templates directory." % args.mcc)

    found = find_assets(args.mcc)
    if args.only:
        found = [f for f in found if KINDS[f[0]][0] == args.only]
    if not found:
        sys.exit("no .mbp, .mfs or .mfw members under %s" % args.mcc)

    if not args.list and not args.outdir:
        ap.error("--outdir is required unless you pass --list")

    rows, wrote = [], []
    for ext, dirpath, members in found:
        kind, container = KINDS[ext]
        label = bank_label(args.mcc, dirpath)
        try:
            items, used, empty = read_members(members)
        except ValueError as exc:
            print("  SKIP %-13s %-22s %s" % (kind, label, exc), file=sys.stderr)
            continue
        if used == 0:
            # MCC ships `Device/Reserved` as a full bank of empty slots -- a
            # template for the hardware, not content. Writing four empty bank
            # files is noise, and an empty bank is the one thing nobody can
            # use.
            print("  %-11s %-22s %3d members, all empty -- nothing to import"
                  % (kind, label, len(members)), file=sys.stderr)
            continue
        rows.append(dict(kind=kind, label=label, members=len(members),
                         used=used, empty=empty,
                         source=os.path.relpath(dirpath, args.mcc)))
        print("  %-11s %-22s %3d members, %3d used, %3d empty"
              % (kind, label, len(members), used, empty), file=sys.stderr)
        if args.list:
            continue
        outdir = (os.path.join(args.outdir, kind) if args.by_kind
                  else args.outdir)
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, label + container)
        bank.write_bank(out, items, top="MFImport",
                        subdir="01-%s-A" % label)
        wrote.append(out)
        print("       -> %s" % out, file=sys.stderr)

    if args.list:
        print("\n%d export(s) available. Re-run with --outdir DIR to write them."
              % len(rows), file=sys.stderr)
    else:
        print("\nwrote %d export file(s) to %s" % (len(wrote), args.outdir),
              file=sys.stderr)
    jsonout.result(exports=rows, written=[os.path.basename(p) for p in wrote],
                   mcc=args.mcc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
