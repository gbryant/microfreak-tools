"""Read a preset off the EMULATOR with the shipping code, and re-encode it.

The first test in this package, and it needs no instrument: `mfemu serve`
boots the firmware once and leaves it running, and this attaches to that.

    mfemu serve --preset 7=Speech          # in one shell, leave it up
    uv run python tests/test_preset_roundtrip.py

What it exercises is the whole stack the tool actually ships:

    READ        `preset.read.read_preset` -- the same function
                `mfreak preset read` calls against hardware, unmodified --
                drives 146 request/reply round trips over emulated USB-MIDI
                against the firmware's own SysEx dispatcher.
    DECODE      the 4672 wire bytes unpack to a 4088-byte parameter stream.
    PARSE       that stream parses as TLV, and the firmware's own loader rules
                (`validate_loadable`) find nothing wrong with it.
    RE-ENCODE   serialising the parsed records and repacking them reproduces
                the wire bytes EXACTLY. This is the half that could not be
                tested without a device: it holds our codec against bytes the
                firmware produced, rather than against bytes we produced.

It deliberately does not assume which preset is in the slot. Everything
asserted is an invariant of any preset the firmware will hand over, so the
test stays true whatever `mfemu serve` was started with.
"""
import os
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import microfreak.preset.fields as PF                                # noqa: E402
import microfreak.preset.tlv as tlv                                  # noqa: E402
from microfreak.midi import checksum_free_seq, open_synth            # noqa: E402
from microfreak.preset.read import read_preset                       # noqa: E402

# A PANEL number, like every other preset number a user types, converted once
# here. It was MF_TEST_SLOT, a 0-based index, which after the 2026-10-19 panel
# flip disagreed with this file's own skip text: that prints
# `mfemu serve --preset %d` with the same number, and `--preset` has taken
# panel numbers since the flip -- so the instructions set up index 6 and the
# test then read index 7.
PRESET = int(os.environ.get("MF_TEST_PRESET", "8"))
SLOT = PRESET - 1
WIRE_LEN, DECODED_LEN = 4672, 4088


def main():
    if not os.environ.get("MFREAK_EMULATOR"):
        import tempfile
        session = os.path.join(tempfile.gettempdir(), "mfemu-session.json")
        if not os.path.exists(session):
            print("SKIP - no emulator is running, and this test does not touch")
            print("       real hardware on its own initiative.")
            print("""
  Start one and run this again:

      mfemu serve --preset %d=Speech        (leave it running)
      MFREAK_EMULATOR=1 uv run python tests/test_preset_roundtrip.py

  mfemu is the sibling package, microfreak-emu. Nothing here needs an
  instrument -- that is the point of it.""" % PRESET)
            return 0
        os.environ["MFREAK_EMULATOR"] = "1"

    synth = open_synth(timeout=120.0)
    fails = []

    def chk(ok, label, detail=""):
        print("  %-4s %-44s %s" % ("ok" if ok else "FAIL", label, detail))
        if not ok:
            fails.append(label)

    print("\n1. read preset %d (slot %d) with the shipping protocol code"
          % (PRESET, SLOT))
    try:
        name, wire = read_preset(synth, SLOT, checksum_free_seq(), verbose=False)
    except Exception as exc:
        print("  FAIL read_preset raised %r" % exc)
        print("\n  If this timed out, check `mfemu serve` enumerated USB: the")
        print("  device answers SysEx over USB, not over DIN, so a machine")
        print("  started with --no-usb will accept requests and never reply.")
        return 1
    chk(len(wire) == WIRE_LEN, "the payload is %d wire bytes" % WIRE_LEN,
        "%d" % len(wire))
    chk(bool(name) and all(0x20 <= ord(c) < 0x7F for c in name),
        "the name record decodes to printable text", repr(name))

    print("\n2. decode and parse")
    decoded = bytes(tlv.decode_7to8(wire))
    chk(len(decoded) == DECODED_LEN, "7-to-8 decodes to %d bytes" % DECODED_LEN,
        "%d" % len(decoded))
    records, consumed = tlv.parse(decoded)
    groups = tlv.groups(decoded)
    chk(len(groups) > 0, "the parameter stream parses as TLV",
        "%d modules, %d records, %d bytes consumed"
        % (len(groups), len(records), consumed))
    rows = PF.rows_from_payload(wire)
    chk(PF.osc_engine(rows) is not None, "the oscillator engine reads back",
        repr(PF.osc_engine(rows)))

    print("\n3. the firmware's own loader rules")
    problems = tlv.validate_loadable(decoded, known=PF.ASSIGN_PARAMS,
                                     modules=PF.MODULE_TABLE)
    fatal = [p for p in problems if p[0] == "fatal"]
    chk(not fatal, "nothing the loader would reject",
        "%d problem(s)%s" % (len(problems),
                             "" if not problems else " " + str(problems[:2])))

    print("\n4. re-encode, and compare against what the firmware sent")
    again = bytes(tlv.encode_7to8(bytes(tlv.serialize(records))
                                  + decoded[consumed:]))
    chk(again == wire, "our codec reproduces the firmware's bytes exactly",
        "%d of %d bytes identical"
        % (sum(1 for a, b in zip(again, wire) if a == b), len(wire)))

    print("\n5. a second client, and replies that track the slot asked for")
    # The regression guard for a transport bug that shipped undetected: an
    # attached client read the emulator's USB capture from byte 0, so it found
    # every reply the machine had ever sent. Sequence numbers did not save it,
    # because a new client starts its own `checksum_free_seq()` and reuses the
    # previous one's numbers. The result was silent and specific: the FIRST
    # slot read after boot was correct, and every slot after that returned
    # those same bytes. Reading an empty slot first inverted it -- every later
    # read came back 'Init' with an erased page, slot 7 included.
    #
    # This attaches a SECOND client with its own fresh sequence counter, the
    # way a second `mfreak` invocation does, and asks for a different slot.
    # What is asserted is not which preset is there -- that depends on how
    # `mfemu serve` was started -- but that the reply is about the slot
    # requested. The firmware echoes it at bytes 9 and 10.
    #
    # The first client is closed first, deliberately. Renode's socket terminal
    # and its Monitor each accept exactly ONE client, so two open at once is
    # not a thing a user can do -- and two SEQUENTIAL invocations is exactly
    # the case that was broken.
    other = SLOT + 1 if SLOT + 1 <= 511 else SLOT - 1
    synth.close()
    synth2 = open_synth(os.environ.get("MF_PORT", "MicroFreak"), timeout=6.0)
    seq2 = checksum_free_seq()
    try:
        from microfreak.preset.read import read_name_record
        hi, lo = divmod(other, 128)
        rep = read_name_record(synth2, other, seq2)
        chk(rep[9] == hi and rep[10] == lo,
            "a fresh client's reply is about the slot it asked for",
            "asked %d (%02X %02X), reply says %02X %02X"
            % (other, hi, lo, rep[9], rep[10]))
    finally:
        synth2.close()

    print()
    if fails:
        print("FAIL: %s" % ", ".join(fails))
        return 1
    print("PASS - the shipping read path works against the emulator, and our")
    print("       codec round-trips bytes the FIRMWARE produced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
