#!/usr/bin/env python3
"""Read one preset's name and patch data off the instrument.

    mfreak preset read 8
    mfreak preset read 8 --out bass.mfpz

--out saves it as an MCC preset file, with its name and category. It
restores with `preset write N --file bass.mfpz`.
"""
import argparse
import sys

from microfreak.midi import open_synth, build, checksum_free_seq
from microfreak import jsonout
from microfreak import mccfile
from microfreak import panel

# Byte 19 of the 45-byte name record: the save-screen category.
NR_CATEGORY = 19


def category_of(record):
    """-> a name record's save-screen category, 0 for an empty slot, whose
    record is 0xFF filler (byte 12 bit 0x08 marks it empty)."""
    return 0 if record[12] & 0x08 else record[NR_CATEGORY] & 0x7F


def schema_of(record):
    """-> the schema byte the instrument stored with the preset (byte 20), or
    None for an empty slot. Its low nibble differs between firmware versions,
    so it is copied, not derived from the payload."""
    return None if record[12] & 0x08 else record[20]


def name_of(reply):
    s = ""
    for b in reply[21:-1]:
        if b == 0:
            break
        s += chr(b) if 0x20 <= b < 0x7F else "?"
    return s


def read_name_record(synth, slot, seqgen):
    """Sends the tail=00 name request and returns the raw 45-byte reply.

    The reply echoes the slot it is about, at bytes 9 and 10, and this matches
    on that as well as on the sequence number. Sequence numbers alone are not
    enough to identify a reply: a tool invocation starts a fresh
    `checksum_free_seq()`, so two runs against one long-lived instrument reuse
    the same numbers, and anything that can re-present an older reply will
    satisfy a seq-only predicate. That is not hypothetical -- it is how a
    transport bug returned slot 7's preset for every slot asked for, silently,
    on 2026-10-15. Checking the echo costs nothing and makes the wrong reply
    time out loudly instead of being believed.
    """
    hi, lo = divmod(slot, 128)
    seq = next(seqgen)
    synth.send(build(seq, 0x03, 0x19, hi, lo, 0x00))
    return synth.recv(lambda m: len(m) == 45 and m[7] == 0x23 and m[6] == seq
                      and m[9] == hi and m[10] == lo)


def read_preset(synth, slot, seqgen, verbose=False):
    hi, lo = divmod(slot, 128)

    reply = read_name_record(synth, slot, seqgen)
    name = name_of(reply)
    if verbose:
        print(f"  name: {name!r}", file=sys.stderr)

    seq = next(seqgen)
    synth.send(build(seq, 0x03, 0x19, hi, lo, 0x01))
    synth.recv(lambda m: len(m) == 10 and m[7] == 0x00 and m[8] == 0x15 and m[6] == seq)

    payload = bytearray()
    chunk_count = 0
    while True:
        seq = next(seqgen)
        synth.send(build(seq, 0x01, 0x18, 0x00))
        chunk = synth.recv(
            lambda m: len(m) == 42 and m[7] == 0x20 and m[6] == seq
        )
        payload.extend(chunk[9:41])
        chunk_count += 1
        if chunk[8] == 0x17:
            break
        if chunk_count > 1000:
            raise RuntimeError("block did not terminate after 1000 chunks — aborting")

    if verbose:
        print(f"  {chunk_count} chunks, {len(payload)} bytes", file=sys.stderr)

    return name, bytes(payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("preset", type=int, metavar="PRESET",
                    help="which preset (1-512)")
    ap.add_argument("--port", default="MicroFreak", help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--out", metavar="FILE.mfpz", help="save it as an MCC preset file")
    ap.add_argument("--timeout", type=float, default=6.0, help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    # Panel number in, 0-based slot out, straight after parsing so
    # everything below still sees the index it always did.
    args.slot = panel.to_slot(ap, args.preset, panel.PRESETS, "preset")
    if args.out:
        try:
            mccfile.check_ext(args.out, "preset")
        except mccfile.FileError as exc:
            ap.error(str(exc))

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        record = read_name_record(synth, args.slot, seqgen)
        category, schema = category_of(record), schema_of(record)
        name, payload = read_preset(synth, args.slot, seqgen, verbose=True)
    finally:
        synth.close()

    print(f"preset {args.slot + 1}: {name!r}  ({len(payload)} bytes)")
    jsonout.result(slot=args.slot, panel=args.slot + 1, name=name,
                   bytes=len(payload), out=args.out)
    if args.out:
        mccfile.write_one(args.out,
                          mccfile.preset_archive(name, category, payload, schema),
                          "preset")
        print(f"wrote {args.out}")
    else:
        for off in range(0, len(payload), 16):
            chunk = payload[off:off + 16]
            h = " ".join(f"{b:02X}" for b in chunk)
            a = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
            print(f"  {off:04d}  {h:<47}  |{a}|")


if __name__ == "__main__":
    main()
