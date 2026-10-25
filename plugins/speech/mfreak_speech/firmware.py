"""Stock firmware that also takes speech vocabulary uploads over SysEx.

    mfreak fw patch speech MicroFreak_Firmware_Update_5_0_0_2084.mff --out speech.mff
    mfreak fw flash speech.mff

Then `mfreak speech say` and `mfreak speech banks` put words on the Speech
engine. The boot screen reads PATCHED speech18, and asked for its version over
MIDI it answers 5.0.S, build 18. Part of the factory vocabulary in bank 4 is
given up to make room. To go back, flash the stock .mff.
"""
import argparse

from microfreak.firmware import patching as F
from microfreak.firmware.peek import PEEK_BASE, PEEK_OPCODE, PEEK_SPAN

import mfreak_speech.sysex as X


def add_arguments(ap):
    ap.add_argument("--peek-base", type=lambda x: int(x, 0), default=None,
                    metavar="ADDR", help=argparse.SUPPRESS)


def build(img, args):
    rep = X.apply_patch(img, peek_base=args.peek_base)
    pb = rep["peek_base"]
    print("handler   0x%08X  %d bytes, opcode 0x%02X: the frame upload"
          % (rep["handler"] + (X.OPCODE,)))
    print("init stub 0x%08X  %d bytes" % rep["init_stub"])
    print("detour    0x%08X  4 bytes  (SpeechEngine::Init argument setup)"
          % rep["init_patch"])
    print("peek      0x%08X  %d bytes, opcode 0x%02X: read-only, window "
          "0x%08X..0x%08X%s"
          % (rep["peek"] + (PEEK_OPCODE, pb, pb | PEEK_SPAN,
                            "" if pb == PEEK_BASE else "  (not the default)")))
    print("trampoline 0x%08X 4 bytes  (spends opcodes 0x%02X and 0x%02X)"
          % (rep["tramp"][0], F.SPENT[0], F.SPENT[1]))
    F.report_banner(img)
    F.report_room(rep["trimmed"], rep["used"])
