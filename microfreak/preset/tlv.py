#!/usr/bin/env python3
"""Show every module and parameter in a preset file.

The serializer at 0x08054A68 (v5 firmware) emits, for every record, a single
header byte built by two bitfield inserts:

    bfi r3, r0, #0, #5     bits[4:0] = length of what follows
    bfi r2, r3, #5, #3     bits[7:5] = record type

so  header = (type << 5) | length.

That one line explains the whole format, including two things that looked like
magic constants:

    0x23  '#'  type 1, len 3  -- a module tag; every module tag is 3 chars,
                                 so the header always encodes as '#'
    0x63  'c'  type 3, len 3  -- a value; every value is exactly 3 bytes,
                                 so the header always encodes as 'c'
    0x40  '@'  type 2, len 0  -- end-of-module terminator

Types seen: 1 = module tag, 2 = parameter name, 3 = value.

This supersedes an earlier name-anchor approach, which searched
for known field names and measured the gap to the next one. That measurement
included the framing bytes, which is why fields appeared to have 3/5/8-byte
widths: every value is 3 bytes, and a field measuring 5 is simply the last
parameter in its module (3 value bytes + the 0x40 terminator). Nothing about
the field itself varies.

See microfreak-emu/docs/FORMATS.md.
"""
import argparse
import sys
from microfreak import jsonout

T_MODULE, T_NAME, T_VALUE = 1, 2, 3
END_OF_MODULE = 0x40          # type 2, len 0
VALUE_LEN = 3


class Record:
    __slots__ = ("offset", "header", "type", "length", "body")

    def __init__(self, offset, header, body):
        self.offset = offset
        self.header = header
        self.type = header >> 5
        self.length = header & 0x1F
        self.body = body

    def text(self):
        return self.body.decode("ascii", errors="replace")

    def __repr__(self):
        b = self.text() if self.type in (T_MODULE, T_NAME) else self.body.hex(" ")
        return "Record(%d, 0x%02X, t%d, len%d, %r)" % (
            self.offset, self.header, self.type, self.length, b)


def parse(payload):
    """Walks the TLV stream. Returns (records, end_offset).

    Stops at the first 0x00 header, which is where the parameter stream ends.
    Everything after end_offset is NOT TLV -- on a standard 4088-byte decoded
    block the stream runs to ~2025 and the remainder is sequencer step data
    with no known mapping.
    """
    records, i = [], 0
    while i < len(payload):
        header = payload[i]
        if header == 0:
            break
        rtype, length = header >> 5, header & 0x1F
        if i + 1 + length > len(payload):
            break
        body = payload[i + 1:i + 1 + length]
        # Module tags and parameter names are ASCII. Anything else means we
        # have walked off the end of the parameter stream into the trailing
        # sequencer region, which is not TLV -- stop rather than inventing
        # records out of it.
        if rtype in (T_MODULE, T_NAME) and length:
            if not all(0x20 <= c < 0x7F for c in body):
                break
        elif rtype not in (T_MODULE, T_NAME, T_VALUE):
            break
        records.append(Record(i, header, body))
        i += 1 + length
        # A type-1 record with no body is the end-of-stream marker. Anything
        # after it is 0xFF fill, not TLV -- stop, or the walk reads the fill
        # as a record.
        if rtype == T_MODULE and length == 0:
            break
    return records, i


def serialize(records):
    """Records -> bytes. Exact inverse of parse(); headers are recomputed from
    each record's type and body length rather than replayed, so a round trip
    proves the header rule rather than assuming it.
    """
    out = bytearray()
    for r in records:
        if len(r.body) > 0x1F:
            raise ValueError("body too long for a 5-bit length: %r" % (r.body,))
        if not 0 <= r.type <= 7:
            raise ValueError("type out of range: %d" % r.type)
        out.append((r.type << 5) | len(r.body))
        out += r.body
    return bytes(out)


def build(groups, end_of_stream=True):
    """[(module, [(name, value_bytes), ...])] -> a TLV byte stream.

    Takes grouped modules rather than flat triples because a module can be
    present with **no** parameters -- `SyA` usually is -- and a flat
    (module, name, value) list cannot represent that. Emitting it still costs
    5 bytes on the wire, so dropping it breaks a byte-exact round trip.
    """
    out = bytearray()
    for module, rows in groups:
        out.append((T_MODULE << 5) | len(module))
        out += module.encode("ascii")
        for name, value in rows:
            out.append((T_NAME << 5) | len(name))
            out += name.encode("ascii")
            if len(value) != VALUE_LEN:
                raise ValueError("value must be %d bytes, got %d"
                                 % (VALUE_LEN, len(value)))
            out.append((T_VALUE << 5) | VALUE_LEN)
            out += value
        out.append(END_OF_MODULE)
    if end_of_stream:
        out.append(T_MODULE << 5)          # 0x20, type 1 length 0
    return bytes(out)


def groups(payload):
    """-> [(module, [(name, value_bytes), ...])], preserving empty modules."""
    records, _ = parse(payload)
    out, current, rows, pending = [], None, [], None
    for r in records:
        if r.type == T_MODULE and r.length:
            if current is not None:
                out.append((current, rows))
            current, rows, pending = r.text(), [], None
        elif r.type == T_NAME and r.length:
            pending = r.text()
        elif r.type == T_VALUE and pending is not None:
            rows.append((pending, r.body))
            pending = None
    if current is not None:
        out.append((current, rows))
    return out


def fields(payload):
    """-> [(module, name, value_bytes)] for every parameter in the stream."""
    records, _ = parse(payload)
    out, module = [], None
    pending = None
    for r in records:
        if r.type == T_MODULE and r.length:
            module = r.text()
        elif r.type == T_NAME and r.length:
            pending = r.text()
        elif r.type == T_VALUE and pending is not None:
            out.append((module, pending, r.body))
            pending = None
    return out


def main():
    # No `prog=` here: the dispatcher sets argv[0] to the command the user
    # actually typed.
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="    mfreak preset decode bass.mfpz\n"
               "    mfreak preset decode Factory6.0.mfprojz --preset 12\n\nOffline.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", metavar="FILE",
                    help="an MCC preset file (.mfpz), or a bank with --preset")
    ap.add_argument("--preset", type=int, metavar="N",
                    help="which preset of a .mfprojz or .mfbz")
    a = ap.parse_args()
    from microfreak import mccfile
    import microfreak.bank as B
    try:
        kind, bank = mccfile.kind_of(a.file)
        if kind != "preset":
            raise mccfile.FileError("%s is a %s file, not a preset" % (a.file, kind))
        if bank:
            if a.preset is None:
                ap.error("%s is a bank. Pass --preset N." % a.file)
            found = [it for it in B.read_bank(a.file)
                     if it.slot == a.preset and not it.is_empty()]
            if not found:
                raise mccfile.FileError("%s has no preset %d" % (a.file, a.preset))
            item = found[0]
        else:
            if a.preset is not None:
                ap.error("--preset is for a bank; %s holds one preset" % a.file)
            item = mccfile.read_one(a.file, "preset")
    except mccfile.FileError as exc:
        sys.exit(str(exc))
    print("%r" % item.name)
    payload = bytes(b & 0xFF for b in decode_7to8(list(item.wire())))
    records, end = parse(payload)
    if not records:
        sys.exit("no TLV records in %s" % a.file)
    rows = fields(payload)
    modules = [r.text() for r in records if r.type == T_MODULE and r.length]
    print("%d records, stream ends at %d of %d bytes" % (len(records), end, len(payload)))
    print("%d modules: %s" % (len(modules), " ".join(modules)))
    print("%d parameters\n" % len(rows))
    jsonout.result(
        records=len(records), stream_end=end, bytes=len(payload),
        modules=modules,
        parameters=[{"module": m, "name": n, "value": v.hex(" ")}
                    for m, n, v in rows])
    for module, name, value in rows:
        print("  %-4s %-12s %s" % (module, name, value.hex(" ")))


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Sequencer (region 3)
#
# A decoded preset block is three regions: the TLV parameter stream, 0xFF
# fill, then the sequencer at a fixed offset. The sequencer is a flat array of
# 16-byte step records -- two sequences of 64 steps, separated by an
# ff-filled marker.
#
#   bytes  0-3   four note slots     MIDI note, or 0xFB / 0xFF / 0x00 = empty
#   bytes  4-7   four per-note VELOCITIES, 7-bit (max 0x7F), 0x64 = 100 on
#                rest steps. Confirmed on hardware: recording the device's
#                own MIDI output while a sequence played reproduced all 12
#                stored (note, velocity) pairs exactly, including velocities
#                of 1 and 2. Those are inaudible not because the field is
#                something else, but because the MicroFreak's amp is
#                envelope-driven -- velocity is a mod source that has to be
#                routed before it affects level.
#   bytes  8-11  four automation track values, 8-bit (0x00-0xFF)
#   byte   12    gate state: 0 = rest, 1 = normal note, 2 = TIED into the
#                next step. Confirmed by differential test on hardware:
#                adding a tie to one step flipped exactly that step's byte 12
#                from 1 to 2, and the following step was rewritten to repeat
#                the same note at the same velocity with byte 12 = 1. A tie
#                therefore occupies both steps -- it overwrites whatever the
#                next step held.
#   byte   13    bitmask of which automation tracks are active this step.
#                Verified on 24849 of 24960 steps; every mismatch is a track
#                legitimately holding value 0, which is exactly why the mask
#                exists -- do not infer it from non-zero-ness.
#   bytes 14-15  always zero across all 24960 steps. Reserved.
# ---------------------------------------------------------------------------

SEQ_OFFSET = 1980       # 353 of 390 standard presets; 1984 in 29 more
SEQ_STEPS = 64
STEP_LEN = 16
NOTE_EMPTY = (0x00, 0xFB, 0xFF)
AUTOMATION_TRACKS = 4


class Step:
    __slots__ = ("notes", "velocities", "automation", "state", "raw")

    def __init__(self, raw):
        self.raw = raw
        self.notes = [n for n in raw[0:4] if n not in NOTE_EMPTY]
        self.velocities = list(raw[4:8])[:len(self.notes)]
        # byte 13 says which tracks are live; a live track may hold 0
        self.automation = {i: raw[8 + i] for i in range(AUTOMATION_TRACKS)
                           if raw[13] >> i & 1}
        self.state = raw[12]

    def is_rest(self):
        return not self.notes

    def is_tied(self):
        """True if this step's note continues into the next one. The next
        step repeats the same note/velocity with state 1."""
        return self.state == 2

    def __repr__(self):
        return "Step(notes=%s, vel=%s, auto=%s, state=%d)" % (
            self.notes, self.velocities, self.automation, self.state)


def sequence(payload, index=0, offset=SEQ_OFFSET):
    """-> [Step] for sequence 0 or 1.

    Sequence 1 sits after a separator record and is offset by 2 bytes within
    the record grid; that shift is applied here but is not understood, so
    treat sequence 1 as less certain than sequence 0.
    """
    base = offset + (0 if index == 0 else SEQ_STEPS * STEP_LEN + STEP_LEN + 2)
    return [Step(payload[base + k * STEP_LEN: base + (k + 1) * STEP_LEN])
            for k in range(SEQ_STEPS)]


# ---------------------------------------------------------------------------
# Corpus loading (moved here from an earlier decoder, since removed --
# its name-anchor field model was superseded by the TLV parser above).
# ---------------------------------------------------------------------------
import json


def load_decoded_presets(jsonl_path, return_raw=False):
    """Returns {(hi,lo): decoded_real_bytes} for every clean (non-straggler)
    preset data block in the capture. With return_raw=True, returns
    {(hi,lo): raw_wire_bytes} instead -- the un-decoded 4672-byte payload,
    e.g. for `preset.fields`'s [chunk, offset] addressing."""
    msgs = []
    with open(jsonl_path) as f:
        for line in f:
            r = json.loads(line)
            if r["type"] != "sysex":
                continue
            b = [int(x, 16) for x in r["raw"].split()]
            msgs.append((r["dir"], b))

    selects = [(i, b[9], b[10]) for i, (d, b) in enumerate(msgs)
               if d == "MCC -> dev" and len(b) == 13 and b[7] == 0x03
               and b[8] == 0x19 and b[11] == 1]

    def payload_at(i):
        payload = bytearray()
        j = i + 1
        while True:
            d, b = msgs[j]
            if d == "dev -> MCC" and len(b) == 42 and b[7] == 0x20:
                payload.extend(b[9:41])
                if b[8] == 0x17:
                    break
            j += 1
        return bytes(payload)

    out = {}
    for i, hi, lo in selects:
        raw = payload_at(i)
        if return_raw:
            out[(hi, lo)] = raw
        else:
            real = decode_7to8(list(raw))
            out[(hi, lo)] = bytes(b & 0xFF for b in real)
    return out



def decode_7to8(wire):
    """Wire bytes -> the real bytes the parser above expects."""
    out = bytearray()
    for g in range(0, len(wire) - 7, 8):
        header = wire[g]
        for i in range(7):
            out.append(wire[g + 1 + i] | (((header >> i) & 1) << 7))
    return bytes(out)


def encode_7to8(payload):
    """The inverse: real bytes -> the wire form the device is sent.

    Needed to *write* a preset, not just read one.  `preset.read` returns
    the wire form and `preset.write` sends it, but everything that reads a
    saved patch -- `presetlib.read_mfpz`, the MCC captures -- hands back the
    decoded stream, so anything going back to a device has to be re-encoded.

    Eight wire bytes per seven real ones: a header carrying their top bits,
    low bit first, then the seven bytes with bit 7 cleared.  A short final
    group is zero-padded, which is what makes the length come out at the
    4088 -> 4672 the protocol expects.
    """
    out = bytearray()
    for g in range(0, len(payload), 7):
        group = payload[g:g + 7]
        header = 0
        for i, b in enumerate(group):
            header |= ((b >> 7) & 1) << i
        out.append(header)
        out.extend(b & 0x7F for b in group)
        out.extend(b"\x00" * (7 - len(group)))
    return bytes(out)


def validate_loadable(payload, known=None, modules=None):
    """-> list of problems the MicroFreak's own preset loader would hit.

    Implements the acceptance rules read off the loader (`FUN_08054E08`, parse
    loop at `0x0805522E` -- see microfreak-emu/docs/FORMATS.md). The firmware validates
    *framing* and nothing else: it never rejects a parameter's value, it scales
    and clamps whatever 16 bits it is given.

    Two severities, matching the firmware:

      "fatal"  the loader returns an error and the preset does not load --
               a value record whose length is not exactly 3 (`return 2`), or a
               record appearing where a different type was expected
               (`return 1`).
      "skip"   the loader silently drops that one parameter: an unrecognised
               module tag or parameter name, since parameters are addressed by
               NAME and looked up at load time. Not fatal, but the value is
               lost, so a generator producing these is wasting a field.

    `known` is {module: (parameter, ...)}; pass preset_fields.ASSIGN_PARAMS to
    check names. Omit it to check framing only. `modules` is the full module
    tag list (preset_fields.MODULE_TABLE): a tag the firmware knows but
    `known` does not enumerate -- `SyA`, whose panel button and LED parameter
    names are read off a literal pool and so are not verified -- is left
    unchecked rather than reported, since the loader resolves it fine.
    """
    records, _ = parse(payload)
    problems, module, pending = [], None, None
    expect = "module"
    for r in records:
        if r.type == T_MODULE and r.length:
            module = r.text()
            recognised = (module in known if known is not None else True) or \
                         (modules is not None and module in modules)
            if not recognised:
                problems.append(("skip", "unknown module tag %r" % module))
            expect = "name"
            pending = None
        elif r.type == T_NAME:
            if not r.length:                      # end-of-module terminator
                expect = "module"
                pending = None
                continue
            if expect == "value":
                problems.append(("fatal", "%s/%s: name record where a value "
                                 "was expected" % (module, pending)))
            pending = r.text()
            if known is not None and module in known and pending not in known[module]:
                problems.append(("skip", "%s has no parameter %r"
                                 % (module, pending)))
            expect = "value"
        elif r.type == T_VALUE:
            if expect != "value":
                problems.append(("fatal", "value record with no name before it"))
            if r.length != VALUE_LEN:
                problems.append(("fatal", "%s/%s: value is %d bytes, the loader "
                                 "requires exactly %d"
                                 % (module, pending, r.length, VALUE_LEN)))
            expect = "name"
    return problems
