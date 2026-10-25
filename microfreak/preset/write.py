#!/usr/bin/env python3
"""Write a preset to the instrument, replacing what is there.

    mfreak preset write 6 --file bass.mfpz --yes
    mfreak preset write 20 --from 6 --yes             copy preset 6 onto 20
    mfreak preset write 6 --name "New Name" --yes     rename it

It shows what it would write and asks first. --yes skips the question. --file takes an MCC preset
file, and its name and category, unless --name or --category say otherwise.
A preset from older firmware is converted for the instrument's firmware
unless --no-convert.
With no --from or --file, it rewrites the preset's own content, keeping its
name and category unless you change them. The result is read back and
checked unless --no-verify.

This ignores the instrument's Mem protect setting and can overwrite factory
presets. Back up first: `mfreak preset backup FILE.mfprojz`.
"""
import argparse
import sys

from microfreak import confirm

from microfreak.midi import open_synth, build, checksum_free_seq
from microfreak.preset.read import name_of, read_name_record, read_preset
from microfreak import jsonout
from microfreak import mccfile
from microfreak.preset import convert
from microfreak import panel

NAME_FIELD_LEN = 23  # bytes 21..43 of the 45-byte name record
# The instrument keeps only the first 14 characters of a name, and no MCC
# preset has a longer one, so a longer name is refused rather than cut.
NAME_MAX = 14
CHUNK_PAYLOAD_LEN = 32
CHUNKS_PER_PRESET = 146
DATA_LEN = CHUNK_PAYLOAD_LEN * CHUNKS_PER_PRESET  # 4672

# Name-record byte offsets (see microfreak-emu/docs/FORMATS.md).
NR_SLOT_HI = 9    # slot >> 7
NR_SLOT_LO = 10   # slot & 0x7F
NR_FLAGS = 12     # bit 0x08 set => slot is EMPTY
NR_SLOT_LO2 = 17  # region 0 +5: slot % 128
NR_SLOT_HI2 = 18  # region 0 +6: (slot // 128) * 2 as sent, slot // 256 as stored
EMPTY_FLAG = 0x08


NR_REGION0 = 12           # the 45-byte record carries region 0 at [12:44]
NR_CATEGORY = 19          # region 0 +7: the save-screen category
NR_SCHEMA = 20            # region 0 +8: payload schema
SCHEMA_LOW_NIBBLE = 0x3   # what a 5.0.0.2084 device writes; see below


def build_name_record(dest_slot, name, payload, category=0, schema=None,
                      empty=False, stored=False):
    """Build the 45-byte name record from scratch, rather than borrowing one.

    Borrowing was a workaround from before the format was decoded, and it had
    a real failure mode: a payload written under a header of the wrong vintage
    does not render at all -- silently, with the oscillator never called.
    The layout is in microfreak-emu/docs/FORMATS.md:

        +0   0x08 = slot is empty
        +1..+4  zero
        +5   slot % 128
        +6   (slot // 128) * 2
        +7   category, as chosen on the save screen
        +8   schema, the preset's format (preset/convert.py)
        +9.. NUL-terminated name

    That is what MCC sends, in all 512 records of a project send (2026-10-22
    capture). The instrument stores its own form of +0 and +6: bank bit 0x10
    = (slot // 128) & 1 in +0, and slot // 256 in +6. `stored=True` builds
    that form, for laying presets straight into flash.

    The slot index is stored twice, at record bytes 9/10 and again in +5/+6.

    The empty flag must be clear to write a preset. Left set, the device
    acknowledges every chunk but the slot stays empty. Set with no data
    following, it empties the slot, as MCC does for each empty slot in a
    project.

    +7 is the save-screen category, 0x00-0x0B. 0x02 is Keys, 0x03 is Lead.
    """
    if not 0 <= dest_slot <= 511:
        raise ValueError("slot must be 0-511")
    if schema is None:
        import microfreak.preset.tlv as preset_tlv
        decoded = bytes(b & 0xFF for b in preset_tlv.decode_7to8(list(payload)))
        has_voc = any(m == "Voc" for m, _, _ in preset_tlv.fields(decoded))

    region0 = bytearray(32)
    region0[0] = EMPTY_FLAG if empty else 0
    region0[5] = dest_slot % 128
    region0[6] = (dest_slot // 128) * 2
    if stored:
        region0[0] |= ((dest_slot // 128) & 1) << 4
        region0[6] = dest_slot // 256
    region0[7] = category & 0x7F
    # `schema` is the preset's own, when known: its low nibble is the format
    # version, which differs between firmware releases (preset/convert.py).
    region0[8] = (schema if schema is not None
                  else (0x30 if has_voc else 0x10) | SCHEMA_LOW_NIBBLE)

    text = (name or "").encode("ascii", errors="replace")
    if len(text) > NAME_MAX:
        raise ValueError("name too long: max %d ASCII characters" % NAME_MAX)
    region0[9:9 + len(text)] = text

    hi, lo = divmod(dest_slot, 128)
    record = [0xF0, 0x00, 0x20, 0x6B, 0x07, 0x01, 0x00, 0x23, 0x52, hi, lo, 0x00]
    record += list(region0)
    record.append(0xF7)
    assert len(record) == 45, len(record)
    return bytes(record)


def erase_preset(synth, slot, seqgen):
    """Mark a slot empty, as MCC does: a name record with the empty flag set
    and no data. The instrument then reads it back as an empty Init."""
    read_name_record(synth, slot, seqgen)
    record = build_name_record(slot, "Init", None, category=1, schema=0x01,
                               empty=True)
    seq = next(seqgen)
    synth.send([*record[:6], seq, *record[7:]])
    synth.recv(lambda m: len(m) == 10 and m[7] == 0x00 and m[8] == 0x18 and m[6] == seq)


def slot_is_empty(record):
    """True if a name record read back from the device marks an unused slot."""
    return bool(record[NR_FLAGS] & EMPTY_FLAG)


def write_preset(synth, dest_slot, name_record, payload, seqgen, verbose=False):
    if len(payload) != DATA_LEN:
        raise ValueError(f"payload must be exactly {DATA_LEN} bytes, got {len(payload)}")

    hi, lo = divmod(dest_slot, 128)

    # Mirrors MCC: read the current name before overwriting it.
    read_name_record(synth, dest_slot, seqgen)

    seq = next(seqgen)
    msg = [name_record[0], *name_record[1:6], seq, *name_record[7:]]
    synth.send(msg)
    synth.recv(lambda m: len(m) == 10 and m[7] == 0x00 and m[8] == 0x18 and m[6] == seq)
    if verbose:
        print(f"  wrote name record", file=sys.stderr)

    seq = next(seqgen)
    synth.send(build(seq, 0x03, 0x52, hi, lo, 0x01))
    synth.recv(lambda m: len(m) == 10 and m[7] == 0x00 and m[8] == 0x18 and m[6] == seq)

    seq = next(seqgen)
    synth.send(build(seq, 0x00, 0x15))
    synth.recv(lambda m: len(m) == 10 and m[7] == 0x00 and m[8] == 0x18 and m[6] == seq)

    for i in range(CHUNKS_PER_PRESET):
        chunk_payload = payload[i * CHUNK_PAYLOAD_LEN:(i + 1) * CHUNK_PAYLOAD_LEN]
        sub = 0x17 if i == CHUNKS_PER_PRESET - 1 else 0x16
        seq = next(seqgen)
        synth.send(build(seq, 0x20, sub, *chunk_payload))
        synth.recv(lambda m: len(m) == 10 and m[7] == 0x00 and m[8] == 0x18 and m[6] == seq)

    if verbose:
        print(f"  wrote {CHUNKS_PER_PRESET} chunks, {len(payload)} bytes", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", type=int, metavar="PRESET",
                    help="which preset to overwrite (1-512)")
    ap.add_argument("--from", dest="source", type=int, metavar="PRESET",
                    help="copy name and data from this preset")
    ap.add_argument("--name", help="new name (up to %d ASCII characters)" % NAME_MAX)
    # --borrow-header is gone. It copied the source's name record instead of
    # building one, from before the record's layout was known, and its only
    # remaining advantage was keeping the source's category -- which this now
    # does by default. Building was measured byte-exact against a device:
    # all 64 presets in a 5.0.0.2084 factory bank rebuild to the same 32 bytes
    # the instrument holds. Borrowing also had a failure mode building does
    # not, since a payload written under a header of the wrong vintage does
    # not render at all, silently.
    ap.add_argument("--category", type=int, default=None, metavar="N",
                    help="save-screen category, 0-11 (default: keep it)")
    ap.add_argument("--file", metavar="FILE.mfpz",
                    help="an MCC preset file, e.g. from `preset read --out`")
    ap.add_argument("--yes", action="store_true",
                     help="write without asking")
    ap.add_argument("--no-convert", action="store_true",
                    help="send the preset as it is stored, instead of "
                         "converting it for the instrument's firmware")
    ap.add_argument("--no-verify", action="store_true",
                     help="skip reading it back afterwards")
    ap.add_argument("--port", default="MicroFreak", help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0, help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    # Panel numbers in, 0-based slots out. Everything below this line, and
    # every function in this module, still works in 0-based slots.
    args.dest_slot = panel.to_slot(ap, args.dest, panel.PRESETS, "preset")
    args.from_slot = panel.to_slot(ap, args.source, panel.PRESETS, "--from preset")

    # --category is range-checked HERE, with the other argument checks, and not
    # where the value is used. It used to be validated after open_synth() and
    # after the template read, so `--category 99` opened the port, spent up to
    # 146 round trips reading the source preset, and only then printed a usage
    # error. Nothing about the check needs the device.
    if args.category is not None and not 0 <= args.category <= 11:
        ap.error("--category must be 0-11, the save-screen list; got %d"
                 % args.category)

    if args.file and args.from_slot is not None:
        ap.error("--file and --from are alternatives")
    file_item = None
    if args.file:
        try:
            file_item = mccfile.read_one(args.file, "preset")
        except mccfile.FileError as exc:
            sys.exit(str(exc))

    source_slot = args.from_slot if args.from_slot is not None else args.dest_slot

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        try:
            convert.check_protocol(synth)
        except convert.ConvertError as exc:
            sys.exit(str(exc))
        if file_item is not None and not args.no_convert:
            try:
                (file_item,), notes = convert.to_send(synth, [file_item])
            except convert.ConvertError as exc:
                sys.exit("%s: %s" % (args.file, exc))
            for note in notes:
                print(note, file=sys.stderr)
        if file_item is None:
            print(f"reading template from preset {source_slot + 1}...",
                  file=sys.stderr)
        template_record = read_name_record(synth, source_slot, seqgen)
        source_empty = slot_is_empty(template_record)
        if file_item is not None:
            payload = file_item.wire()
        else:
            _, payload = read_preset(synth, source_slot, seqgen, verbose=True)

        # Both the name and the category default to the SOURCE's, so a write
        # changes only what was asked for. They used to default to "" and 0,
        # which made `mfreak preset write N --yes` -- documented as a no-op
        # round trip -- blank the name and force the category to 0 while the
        # parameters round-tripped intact.
        if args.name is not None:
            name = args.name
        elif file_item is not None:
            name = file_item.name
        else:
            name = name_of(template_record)
        if args.category is not None:
            # Range-checked at the parse_args boundary above, not here:
            # region0[7] is written as `category & 0x7F`, so --category 200
            # silently became 72 while --help said 0-11.
            category = args.category
        elif file_item is not None:
            category = mccfile.preset_of(file_item)[1]
        elif source_empty:
            # An erased slot's record is 0xFF padding, not a category.
            category = 0
        else:
            category = template_record[NR_CATEGORY] & 0x7F
        if file_item is not None:
            schema = file_item.archive.schema
        elif source_empty:
            schema = None
        else:
            schema = template_record[NR_SCHEMA]
        name_record = build_name_record(args.dest_slot, name, payload, category,
                                        schema)
        final_name = name_of(name_record)

        if args.dest_slot != source_slot:
            dest_empty = slot_is_empty(read_name_record(synth, args.dest_slot, seqgen))
        else:
            dest_empty = source_empty

        print(f"\nabout to write preset {args.dest_slot + 1}: {final_name!r} "
              f"({len(payload)} bytes)", file=sys.stderr)
        if dest_empty:
            print("  destination is currently empty - clearing its empty flag",
                  file=sys.stderr)
        if source_slot != args.dest_slot:
            print(f"  (cloned from preset {source_slot + 1})", file=sys.stderr)
        if len(payload) != DATA_LEN:
            sys.exit(f"error: payload is {len(payload)} bytes, need exactly {DATA_LEN}")

        if not confirm.ask("Overwrite preset %d?" % (args.dest_slot + 1),
                           args.yes):
            # The preview is exactly when a caller wants to confirm the
            # TARGET before committing, so it reports the same slot/panel
            # the real write does, with written=False to tell them apart.
            jsonout.result(slot=args.dest_slot, panel=args.dest_slot + 1,
                           name=final_name, bytes=len(payload),
                           written=False, verified=None)
            return

        write_preset(synth, args.dest_slot, name_record, payload, seqgen, verbose=True)
        print(f"wrote preset {args.dest_slot + 1}: {final_name!r}")
        jsonout.result(slot=args.dest_slot, panel=args.dest_slot + 1,
                       name=final_name, bytes=len(payload),
                       written=True, verified=None)

        if not args.no_verify:
            print("verifying...", file=sys.stderr)
            got_name, got_payload = read_preset(synth, args.dest_slot, seqgen)
            if got_name == final_name and got_payload == payload:
                jsonout.result(verified=True)
                print("verify OK: read-back matches exactly")
            else:
                jsonout.result(verified=False,
                               read_back_name=got_name,
                               read_back_bytes=len(got_payload))
                print("verify MISMATCH:")
                print(f"  name:    wrote {final_name!r}  read {got_name!r}")
                print(f"  payload: {len(payload)} bytes written, "
                      f"{len(got_payload)} bytes read back, "
                      f"{'differs' if got_payload != payload else 'matches'}")
                # Exit 1. The preset IS written and IS wrong, which is the
                # one outcome a caller must not read as success -- and this
                # is the command where a bad write costs the most. `sample
                # write` and `wavetable write` have always returned 1 here;
                # this one reached the same `finally` and returned None,
                # which cli.py's `mod.main() or 0` turns into exit 0.
                return 1
    finally:
        synth.close()


if __name__ == "__main__":
    main()
