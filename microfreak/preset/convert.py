#!/usr/bin/env python3
"""Convert presets saved by older firmware to a newer firmware's format.

    mfreak preset convert old.mfpz new.mfpz
    mfreak preset convert Factory6.0.mfprojz Factory6.0-5.0.mfprojz
    mfreak preset convert Factory6.0.mfprojz --to 4.0 --check

Each firmware stores the engine against its own engine list, so a preset
saved by older firmware plays the wrong engine on newer firmware.
Converting rewrites the engine for the target's list and adds the
parameters the older format lacks, with the target's Init values. Every
other value, and the sequence, is kept. Presets already in the target
format are copied unchanged, and presets from newer firmware are refused.

--to is the target firmware, 1.0 to 5.0 (default: 5.0). `preset write`
and `preset restore` convert for the instrument's own firmware unless
--no-convert. --check lists what would change and writes nothing.
"""
# The format is the schema byte the firmware stores with every preset; the
# engine count is byte 0 of VCO/Type. Both were read from the Init patch each
# public release saves (microfreak-emu, mfemu panel; microfreak/preset/inits.py):
#
#   schema  engines  parameters  firmware
#   0x10      12         96      1.0.5, 1.1.2
#   0x11      13         98      2.0.3
#   0x30      14        100      2.1.3, 2.1.4
#   0x31      17        106      3.0.6
#   0x32      18        106      4.0.1, 4.0.2, 4.0.3
#   0x33      22        110      5.0.0
#
# Every release stores the engine as (index + 1) * 32767 / count, rounded half
# up, and only ever appended engines and parameters. So an older preset's
# engine index is round(value * count / 32767) - 1, its parameters keep their
# positions within each module, and the assign slots, which name a parameter
# by its position, still point at the same one. Checked on an instrument:
# seven factory presets of counts 12-18, converted for 5.0, showed the engine
# their names describe.
import argparse
import os
import sys

import microfreak.bank as B
import microfreak.preset.fields as PF
import microfreak.preset.tlv as T
from microfreak import jsonout
from microfreak.preset.inits import INITS

FORMATS = {0x10: ("1.x", 12), 0x11: ("2.0", 13), 0x30: ("2.1", 14),
           0x31: ("3.0", 17), 0x32: ("4.0", 18), 0x33: ("5.0", 22)}
ORDER = list(FORMATS)               # oldest first
CURRENT_SCHEMA = 0x33
BY_COUNT = {count: schema for schema, (_, count) in FORMATS.items()}
ENGINES = sorted(PF.ENGINE_VALUES, key=PF.ENGINE_VALUES.get)   # in the order they were added
SEQUENCER = 1980          # decoded offset of the sequence, the same in every format
TARGETS = {"1.0": 0x10, "1.1": 0x10, "2.0": 0x11, "2.1": 0x30, "3.0": 0x31,
           "4.0": 0x32, "5.0": 0x33}


class ConvertError(Exception):
    """A preset this cannot convert. The message says why."""


def schema_for_firmware(major, minor):
    """-> the preset format a firmware version saves, or None if unknown."""
    return TARGETS.get("%d.%d" % (major, minor))


def label(schema):
    return FORMATS[schema][0]


def engine_value(index, count):
    """The value a firmware with `count` engines stores for engine `index`."""
    return int((index + 1) * 32767 / count + 0.5)


def source_format(wire, schema=None):
    """-> the schema byte of the format this preset is in.

    The engine count in the payload decides it; it is what the engine was
    stored against. An unknown count means no public release saved it."""
    dec = T.decode_7to8(list(wire))
    count = None
    for module, rows in T.groups(dec):
        for name, value in rows:
            if (module, name) == ("VCO", "Type"):
                count = value[0]
    if count is None:
        raise ConvertError("no VCO/Type parameter")
    if count not in BY_COUNT:
        raise ConvertError("engine count %d is no public release's; this may "
                           "be custom firmware" % count)
    return BY_COUNT[count]


def convert(wire, schema=None, target=CURRENT_SCHEMA):
    """-> (wire bytes in the `target` format, [what changed]).

    `schema` is the byte stored with the preset, when known; a schema that
    disagrees with the payload's engine count is reported, and the count is
    trusted. A preset already in the target format comes back unchanged.
    """
    source = source_format(wire, schema)
    notes = []
    if schema in FORMATS and schema != source:
        notes.append("its schema byte 0x%02X says %s, its engine count %s"
                     % (schema, label(schema), label(source)))
    if source == target:
        return bytes(wire), notes
    if ORDER.index(source) > ORDER.index(target):
        raise ConvertError("saved by %s firmware, newer than %s"
                           % (label(source), label(target)))
    count, new_count = FORMATS[source][1], FORMATS[target][1]
    dec = T.decode_7to8(list(wire))
    _, end = T.parse(dec)
    groups = T.groups(dec)
    have = {m: dict(rows) for m, rows in groups}
    init = INITS[target]
    out = []
    for module, rows in init:
        mine = have.get(module, {})
        merged = []
        for name, default in rows:
            if name in mine:
                merged.append((name, mine[name]))
            else:
                merged.append((name, default))
                notes.append("added %s/%s" % (module, name))
        known = {n for n, _ in rows}
        merged += [(n, v) for n, v in dict(groups).get(module, []) if n not in known]
        out.append((module, merged))
    for module, rows in groups:
        if module not in {m for m, _ in init}:
            out.append((module, rows))
            notes.append("kept unknown module %s" % module)
    for module, rows in out:
        for k, (name, value) in enumerate(rows):
            if (module, name) == ("VCO", "Type"):
                v = value[1] | ((value[2] & 0x7F) << 8)
                index = min(count, max(1, int(v * count / 32767 + 0.5))) - 1
                new = engine_value(index, new_count)
                rows[k] = (name, bytes([new_count, new & 0xFF, new >> 8]))
                notes.insert(0, "engine %s" % ENGINES[index])
    stream = T.build(out)
    if len(stream) > SEQUENCER:
        raise ConvertError("the converted parameters do not fit")
    # Bytes after the end-of-stream marker are never read; some factory
    # presets keep leftovers of an earlier save there. Only the room the
    # longer stream needs is overwritten.
    new_dec = (stream + b"\xFF" * max(0, end - len(stream))
               + dec[max(end, len(stream)):])
    return T.encode_7to8(new_dec)[:len(wire)], notes


def convert_item(item, target=CURRENT_SCHEMA):
    """-> (archive in the target format, notes). notes is empty when the
    item is in the target format already."""
    schema = item.archive.schema if item.archive else None
    source = source_format(item.wire(), schema)
    if source == target and schema in (None, target):
        return item.archive, []
    wire, notes = convert(item.wire(), schema, target)
    old = item.archive
    # Everything MCC stored is kept, its Characteristics tags included; only
    # the payload and the schema byte change.
    ar = B.Archive(version=old.version, name=old.name, tags=old.tags,
                   values=[v - 256 if v >= 128 else v for v in wire],
                   scalars=old.scalars, abc=(old.abc[0], old.abc[1], str(target)),
                   head=old.head)
    return ar, ["from %s" % label(source)] + notes


INQUIRY = [0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]


def instrument_format(synth):
    """-> (schema byte of the format the instrument saves, "major.minor"),
    or (None, None) when it does not answer the MIDI identity request, and
    (None, version) for a version no public release has."""
    try:
        synth.send(INQUIRY)
        reply = synth.recv(lambda m: len(m) >= 16 and list(m[:5]) == [0xF0, 0x7E, 0x7F, 0x06, 0x02])
    except (TimeoutError, RuntimeError):
        return None, None
    major, minor = reply[12], reply[13]
    return schema_for_firmware(major, minor), "%d.%d" % (major, minor)


def for_instrument(item, target):
    """-> (the Item to send, a one-line note or None). Raises ConvertError
    for a preset newer than the target."""
    ar, notes = convert_item(item, target)
    if not notes:
        return item, None
    added = sum(1 for n in notes if n.startswith("added "))
    engine = next((n for n in notes if n.startswith("engine ")), None)
    note = "converted %r from the %s format to %s%s%s" % (
        item.name, notes[0][5:], label(target), ", " + engine if engine else "",
        ", %d parameters added" % added if added else "")
    return B.Item(item.path, item.slot, ar.version, ar.name, ar.tags,
                  ar.values, ar), note


def check_protocol(synth):
    """Refuse an instrument that reports firmware other than 5.0.

    mfreak's preset requests are 5.0's. 1.0.5 on a real instrument stops
    answering SysEx after one of them, and acts on some of them, changing a
    parameter, so nothing more is sent to it. An instrument that does not
    report its firmware is let through."""
    target, version = instrument_format(synth)
    if version is not None and target != CURRENT_SCHEMA:
        raise ConvertError("the instrument runs firmware %s. Preset commands "
                           "need 5.0: older firmware misreads them." % version)


def to_send(synth, items):
    """Convert `items` for the instrument `synth` talks to.

    -> (items to send, [notes]). Asks the instrument which firmware it runs;
    when it does not say, or runs a version no public release has, the items
    go as stored. Raises ConvertError for one newer than the instrument."""
    target, version = instrument_format(synth)
    if target is None:
        why = ("does not report its firmware" if version is None
               else "runs firmware %s, which no public release has" % version)
        return items, ["the instrument %s, so presets are sent as stored" % why]
    out, notes = [], []
    for it in items:
        try:
            it2, note = for_instrument(it, target)
        except ConvertError as exc:
            raise ConvertError("%r: %s. --no-convert sends it as stored."
                               % (it.name, exc))
        out.append(it2)
        if note:
            notes.append(note)
    return out, notes


def main():
    from microfreak import mccfile
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]) + "\nOffline.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", metavar="FILE", help="a .mfpz, .mfprojz or .mfbz")
    ap.add_argument("out", metavar="OUT", nargs="?",
                    help="the converted file, of the same kind")
    ap.add_argument("--to", default="5.0", choices=sorted(TARGETS),
                    help="the firmware to convert for (default: 5.0)")
    ap.add_argument("--check", action="store_true",
                    help="list what would change and write nothing")
    args = ap.parse_args()
    if not args.out and not args.check:
        ap.error("give OUT, or --check")
    target = TARGETS[args.to]
    try:
        kind, bank = mccfile.kind_of(args.source)
        if kind != "preset":
            raise mccfile.FileError("%s is a %s file, not presets" % (args.source, kind))
        if args.out:
            mccfile.check_ext(args.out, "preset", bank=bank)
            if os.path.abspath(args.out) == os.path.abspath(args.source):
                ap.error("OUT must be a new file")
        items = (B.read_bank(args.source) if bank
                 else [mccfile.read_one(args.source, "preset")])
    except (mccfile.FileError, ValueError) as exc:
        sys.exit(str(exc))

    converted, kept, failed, written = 0, 0, [], []
    for it in items:
        if it.is_empty():
            continue
        where = "%3s  %-14s" % (it.slot or "-", it.name)
        try:
            ar, notes = convert_item(it, target)
        except ConvertError as exc:
            failed.append(dict(slot=it.slot, name=it.name, error=str(exc)))
            print("%s  cannot convert: %s" % (where, exc))
            continue
        if notes:
            converted += 1
            added = sum(1 for n in notes if n.startswith("added "))
            print("%s  %s, %s%s" % (where, notes[0], notes[1] if len(notes) > 1 else "",
                                    ", %d parameters added" % added if added else ""))
        else:
            kept += 1
        written.append((it.slot, ar))
    print("%d converted, %d already %s, %d could not be converted"
          % (converted, kept, label(target), len(failed)))
    if args.out and not args.check:
        if bank:
            project = os.path.splitext(os.path.basename(args.out))[0]
            B.write_bank(args.out, written, top=project,
                         subdir="01-%s-A" % project, ext="mbp")
        elif written:
            mccfile.write_one(args.out, written[0][1], "preset")
        print("wrote %s" % args.out)
    jsonout.result(source=args.source, out=None if args.check else args.out,
                   target=label(target), converted=converted, unchanged=kept,
                   failed=failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
