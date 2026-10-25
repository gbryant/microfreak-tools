"""Read a word of the instrument's memory from a patched build.

Patches that include a memory read answer opcode 0x44 with the magic in `cat`:

    request  F0 00 20 6B 07 01 <seq> 2A 44 <o0> <o1> <o2> F7
    reply    F0 <five septets of the word> <seq> 00 18 F7

The offset o0 | o1<<7 | o2<<14 is ORed onto the base the build was made
with, 0x20000000 (SRAM) unless it says otherwise. Stock firmware does not
reply.
"""
import sys

from microfreak.firmware.patching import OPCODE_MAGIC, WHO_FORMAT, WHO_OPCODE

# MIDI Universal Device Inquiry. Every firmware answers it, and it changes
# nothing, so it is the liveness check.
INQUIRY_REQUEST = bytes([0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7])
INQUIRY_REPLY_PREFIX = bytes([0xF0, 0x7E, 0x7F, 0x06, 0x02])

PEEK_OPCODE = 0x44
PEEK_BASE = 0x20000000
PEEK_SPAN = (1 << 21) - 1        # three septets, so this much from the base
# The STM32F446's 128 KB of SRAM. Reading past it raises a BusFault and hangs
# the instrument, and the handler does not check, so the host must.
SRAM_LO, SRAM_HI = 0x20000000, 0x2001FFFF


def peek_message(addr, seq=0, cat=OPCODE_MAGIC, base=None):
    """One read request. -> raw SysEx bytes.

    `base` is the one the firmware was built with. An address outside its
    window is refused, because the device would read a different one.
    """
    base = PEEK_BASE if base is None else base
    lo, hi = base, base | PEEK_SPAN
    if not lo <= addr <= hi - 3:
        sys.exit("0x%08X is outside this build's peek window "
                 "(0x%08X..0x%08X). Rebuild with --peek-base to move it, and "
                 "pass the same base here." % (addr, lo, hi))
    if base == PEEK_BASE:
        if not SRAM_LO <= addr <= SRAM_HI - 3:
            sys.exit("0x%08X is inside the peek window but outside SRAM "
                     "(0x%08X..0x%08X). Reading unmapped SRAM raises a BusFault "
                     "and hangs the instrument."
                     % (addr, SRAM_LO, SRAM_HI))
    if addr & 3:
        sys.exit("0x%08X is not word-aligned; peek reads a 32-bit word." % addr)
    off = addr - base
    return bytes([0xF0, 0x00, 0x20, 0x6B, 0x07, 0x01, seq, cat, PEEK_OPCODE,
                  off & 0x7F, (off >> 7) & 0x7F, (off >> 14) & 0x7F, 0xF7])


def peek_reply(msg, seq=None):
    """-> the 32-bit word if `msg` is a read reply, else None.

    `seq` rejects a reply to a different request. A blocked read still
    replies, with the seq of the current request and the word of the last
    read that ran, so a caller that must know a value is fresh has to check
    it some other way.
    """
    if len(msg) != 10 or msg[0] != 0xF0 or msg[9] != 0xF7:
        return None
    if msg[7] != 0x00 or msg[8] != 0x18:        # the builder's fixed tail
        return None
    if seq is not None and msg[6] != (seq & 0x7F):
        return None                             # a stale or crossed reply
    v = 0
    for i, b in enumerate(msg[1:6]):
        v |= (b & 0x7F) << (7 * i)
    return v & 0xFFFFFFFF


def who_message(seq=0):
    """The request a patched build answers with its letter and revision."""
    return bytes([0xF0, 0x00, 0x20, 0x6B, 0x07, 0x01, seq & 0x7F,
                  OPCODE_MAGIC, WHO_OPCODE, 0xF7])


def who_reply(msg, seq=None):
    """-> (letter, rev) if `msg` answers who_message, else None."""
    word = peek_reply(msg, seq)
    if word is None or word >> 16:
        return None
    letter, rev, fmt = chr(word & 0x7F), word >> 7 & 0x7F, word >> 14
    if fmt != WHO_FORMAT or not (letter.isascii() and letter.isalpha()):
        return None
    return letter, rev
