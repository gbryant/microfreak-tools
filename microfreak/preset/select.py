#!/usr/bin/env python3
"""Switch the instrument to a preset.

    mfreak preset select 200
    mfreak preset select 200 --reload

Selecting the preset that is already loaded does nothing; --reload selects
another one first to force it. The instrument cannot report which preset is
selected, so check its display if the sound seems wrong.
"""
import argparse
import sys
import time
from microfreak.midi import open_synth
from microfreak import jsonout
from microfreak import panel

BANK_SIZE = 128
MAX_SLOT = 511


def select(synth, slot, reload=False, settle=0.05):
    """Send Bank Select + Program Change for `slot` on an open Synth."""
    if not 0 <= slot <= MAX_SLOT:
        raise ValueError("slot must be 0-%d, got %d" % (MAX_SLOT, slot))
    bank, program = divmod(slot, BANK_SIZE)
    if reload:
        # The firmware ignores a Program Change for the slot already loaded,
        # so bounce through a neighbour to force the page to be re-read.
        other = (slot + 1) % (MAX_SLOT + 1)
        ob, op = divmod(other, BANK_SIZE)
        synth.send([0xB0, 0x00, ob])
        synth.send([0xC0, op])
        time.sleep(settle)
    # Always send the bank, even below 128: the firmware keeps the last bank
    # it was given, so relying on it selects the wrong preset silently.
    # CC#0 only; CC#32 is ignored (microfreak-emu docs/FORMATS.md).
    synth.send([0xB0, 0x00, bank])
    synth.send([0xC0, program])
    time.sleep(settle)
    return bank, program


# open_output() is gone. It opened rtmidi2 directly, so this was one of the
# commands that did NOT honour MFREAK_EMULATOR -- while capabilities.RULES,
# `mfreak`'s own usage and the README all said every command that opens a port
# does. `open_synth` is the single place that decision lives, and routing
# through it needed no other change: Synth.send and EmulatedSynth.send both
# take a list of MIDI bytes, channel messages included.


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[1:]))
    ap.add_argument("preset", type=int, metavar="PRESET",
                    help="which preset (1-%d)" % panel.PRESETS)
    ap.add_argument("--port", default="MicroFreak", help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--reload", action="store_true",
                    help="reload even if this preset is already selected")
    args = ap.parse_args()
    # Panel number in, 0-based slot out, straight after parsing so
    # everything below still sees the index it always did.
    args.slot = panel.to_slot(ap, args.preset, panel.PRESETS, "preset")

    synth = open_synth(args.port)
    name = getattr(synth, "port_name", "the emulator")
    try:
        bank, program = select(synth, args.slot, reload=args.reload)
    finally:
        synth.close()
    # The panel number is what was asked for; bank and program are the wire
    # form it became, and those ARE 0-based -- MIDI program change is.
    print("preset %d -> bank %d, program %d  (CC#0 %d, then C0 %02X) on %s"
          % (args.slot + 1, bank, program, bank, program, name))
    jsonout.result(slot=args.slot, panel=args.slot + 1, bank=bank,
                   program=program, port=name,
                   needed_bank_select=args.slot >= BANK_SIZE)
    if args.slot >= BANK_SIZE:
        print("  note: this sent Bank Select, and the instrument keeps that "
              "bank until another is sent")


if __name__ == "__main__":
    main()
