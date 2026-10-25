"""A read-only diagnostic build: stock firmware that can also read its own
memory, flash included. `mfreak peek dump-bootloader` needs it.

    mfreak fw patch peek MicroFreak_Firmware_Update_5_0_0_2084.mff --out peek.mff
    mfreak fw flash peek.mff

Apart from that, the boot screen reads PATCHED peek18, asked for its version
over MIDI it answers 5.0.P, build 18, and a fault reboots the instrument
instead of hanging it. Flash the stock .mff again when you are done.
"""
import argparse
import struct
import sys

from microfreak.firmware import patching as F
from microfreak.firmware.patching import (BASE, OPCODE_BIAS, OPCODE_MAGIC,
                                          OPCODE_TAIL, REPLY_BUILDER,
                                          REPLY_CODE, assemble, beqw, bw)
from microfreak.firmware.peek import PEEK_BASE, PEEK_OPCODE, PEEK_SPAN

FLAVOUR, REV = "peek", 19
NIBBLE_OPCODE = 0x46             # the nibble-addressed read

# Every fault vector points at the `b .` at 0x0805F89C: NMI, HardFault,
# MemManage, BusFault, UsageFault and every unused IRQ. This build points them
# at `fault_handler` instead.
DEFAULT_HANDLER = 0x0805F89D      # Thumb bit set
VECTOR_TABLE = 0x0800C000         # the image base is the vector table
NUM_VECTORS = 0x1A0 // 4

# The reset handler zeroes .bss only up to 0x2001F230, and 0x2001F238..
# 0x2001FB00 is never touched in normal running, so the dump goes there.
FAULT_DUMP = 0x2001F240
FAULT_MAGIC = 0xFA017DEA
SCB_BASE = 0xE000ED00

# What the handler writes, in order. Read it with the nibble opcode: the
# septet form of its address is one the septet read cannot send safely.
FAULT_FIELDS = ["magic", "ICSR", "CFSR", "HFSR", "MMFAR", "BFAR",
                "stacked_PC", "stacked_LR", "stacked_xPSR", "frame_SP"]


def fault_handler(base, dump_at=FAULT_DUMP, resume=False):
    """-> bytes. Record why the instrument faulted, then reset.

    Writes the ten FAULT_FIELDS words at `dump_at`. With `resume` it instead
    skips the faulting instruction and returns, so the dump can be read at
    once; a reset clears it. A fault that repeats under `resume` loops, which
    hangs the instrument like the stock handler does.
    """
    src = """
        mrs   r0, msp             // handler mode always runs on MSP...
        tst   lr, #4              // ...but the FRAME may be on PSP
        beq   1f
        mrs   r0, psp
    1:  movw  r1, #%#x
        movt  r1, #%#x
        movw  r2, #%#x
        movt  r2, #%#x
        ldr   r3, [r1, #0x04]     // ICSR -- VECTACTIVE says WHICH exception
        str   r3, [r2, #4]
        ldr   r3, [r1, #0x28]     // CFSR
        str   r3, [r2, #8]
        ldr   r3, [r1, #0x2c]     // HFSR
        str   r3, [r2, #12]
        ldr   r3, [r1, #0x34]     // MMFAR
        str   r3, [r2, #16]
        ldr   r3, [r1, #0x38]     // BFAR
        str   r3, [r2, #20]
        ldr   r3, [r0, #24]       // stacked PC -- the faulting instruction
        str   r3, [r2, #24]
        ldr   r3, [r0, #20]       // stacked LR
        str   r3, [r2, #28]
        ldr   r3, [r0, #28]       // stacked xPSR
        str   r3, [r2, #32]
        str   r0, [r2, #36]       // the frame pointer itself
        movw  r3, #%#x
        movt  r3, #%#x
        str   r3, [r2, #0]        // magic LAST: present => the rest is complete
    """ % (SCB_BASE & 0xFFFF, SCB_BASE >> 16,
           dump_at & 0xFFFF, dump_at >> 16,
           FAULT_MAGIC & 0xFFFF, FAULT_MAGIC >> 16)
    if resume:
        src += """
        ldr   r2, [r0, #24]       // stacked PC
        ldrh  r3, [r2]            // its first halfword
        movw  r12, #0xe800        // >= 0xE800 means a 32-bit Thumb encoding
        cmp   r3, r12
        ite   lo
        addlo r2, r2, #2
        addhs r2, r2, #4
        str   r2, [r0, #24]       // resume PAST the faulting instruction
        mvn   r3, #0
        str   r3, [r1, #0x28]     // CFSR is write-1-to-clear
        str   r3, [r1, #0x2c]     // HFSR likewise
        dsb
        isb
        bx    lr                  // EXC_RETURN: back to the thread
    """
    else:
        src += """
        dsb
        movw  r3, #0x0004
        movt  r3, #0x05fa
        str   r3, [r1, #0x0c]     // SCB->AIRCR = SYSRESETREQ
        dsb
    2:  b     2b                  // the reset lands before this spins twice
    """
    return assemble(src)


def decode_fault(words):
    """-> a readable dump, or None if the magic is absent."""
    if not words or words[0] != FAULT_MAGIC:
        return None
    out, icsr = {}, words[1]
    for name, v in zip(FAULT_FIELDS, words):
        out[name] = v
    out["vectactive"] = icsr & 0x1FF
    out["exception"] = {2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault",
                        6: "UsageFault"}.get(icsr & 0x1FF,
                                             "IRQ %d" % ((icsr & 0x1FF) - 16))
    cfsr = out["CFSR"]
    out["cfsr_bits"] = [n for b, n in (
        (0, "IACCVIOL"), (1, "DACCVIOL"), (3, "MUNSTKERR"), (4, "MSTKERR"),
        (7, "MMARVALID"), (8, "IBUSERR"), (9, "PRECISERR"), (10, "IMPRECISERR"),
        (11, "UNSTKERR"), (12, "STKERR"), (15, "BFARVALID"),
        (16, "UNDEFINSTR"), (17, "INVSTATE"), (18, "INVPC"), (19, "NOCP"),
        (24, "UNALIGNED"), (25, "DIVBYZERO")) if cfsr & (1 << b)]
    out["hfsr_bits"] = [n for b, n in (
        (1, "VECTTBL"), (30, "FORCED"), (31, "DEBUGEVT"))
        if out["HFSR"] & (1 << b)]
    return out


def peek_only_handlers(base, peek_base=None, letter="P", rev=None):
    """-> bytes: two read handlers and the identity, entered from the dispatcher.

        opcode 0x44  three septets, ORed onto `peek_base`
        opcode 0x46  eight nibbles, high first: the full 32-bit address
        opcode 0x48  the build's letter and revision, F.who_word

    Both need the magic in `cat`. A message without it still gets a reply, so
    the host can tell a refused read from a hung instrument.
    """
    pb = PEEK_BASE if peek_base is None else peek_base
    src = """
        cmp   r2, #%#x            // the nibble opcode, biased
        nop                       //   -> beq.w the nibble handler
        nop

        ldrb  r0, [r5, #6]        // magic guard
        cmp   r0, #%#x
        bne   8f
        cmp   r2, #%#x            // 0x48, biased
        beq   4f

        ldrb  r0, [r5, #8]        // --- 0x44: three septets
        ldrb  r1, [r5, #9]
        ldrb  r2, [r5, #10]
        orr   r0, r0, r1, lsl #7
        orr   r0, r0, r2, lsl #14
        orr   r0, r0, #%#x
        b     7f

    4:  movw  r0, #%#x            // --- 0x48: who this is
        b     5f

    9:  ldrb  r0, [r5, #6]        // --- 0x46: eight nibbles, full 32 bits
        cmp   r0, #%#x
        bne   8f
        movs  r0, #0
        movs  r2, #8
        add   r1, r5, #8
    6:  ldrb  r3, [r1]
        and   r3, r3, #0x0f
        orr   r0, r3, r0, lsl #4
        adds  r1, r1, #1
        subs  r2, r2, #1
        bne   6b

    7:  ldr   r0, [r0]            // the one load, shared
    5:  movs  r2, #5
        add   r1, r4, #1
    1:  and   r3, r0, #0x7f
        strb  r3, [r1]
        adds  r1, r1, #1
        lsrs  r0, r0, #7
        subs  r2, r2, #1
        bne   1b
        movs  r3, #%#x
        nop                       //   -> b.w REPLY_BUILDER
        nop

    8:  nop                       //   -> b.w OPCODE_TAIL (guard bailed, replies)
        nop
    """ % (NIBBLE_OPCODE - OPCODE_BIAS, OPCODE_MAGIC, F.WHO_OPCODE - OPCODE_BIAS,
           pb, F.who_word(letter, REV if rev is None else rev), OPCODE_MAGIC,
           REPLY_CODE)
    code = bytearray(assemble(src))

    # Patch the three wide branches by locating their placeholders, so a change
    # to the source above cannot silently patch the wrong bytes.
    cmp_nib = bytes([(NIBBLE_OPCODE - OPCODE_BIAS) & 0xFF, 0x2A, 0x00, 0xBF, 0x00, 0xBF])
    at = code.find(cmp_nib)
    if at != 0:
        sys.exit("nibble dispatch placeholder at %d, expected 0" % at)
    # The two trailing placeholders are the last 8 bytes: reply tail, then bail.
    reply_at, bail_at = len(code) - 8, len(code) - 4
    code[2:6] = beqw(base + 2, base + _nibble_entry(code))
    code[reply_at:reply_at + 4] = bw(base + reply_at, REPLY_BUILDER)
    code[bail_at:bail_at + 4] = bw(base + bail_at, OPCODE_TAIL)
    return bytes(code)


def _nibble_entry(code):
    """-> the offset of the nibble arm in `peek_only_handlers`: the second
    `ldrb r0,[r5,#6]`, because that arm repeats the guard.
    """
    pat = assemble("ldrb r0, [r5, #6]")
    first = code.find(pat)
    second = code.find(pat, first + 1)
    if first < 0 or second < 0:
        sys.exit("could not locate the nibble arm's guard")
    return second


def apply(img, peek_base=None, fault=True, fault_resume=False):
    """Patch `img` in place. -> a report dict."""
    code = peek_only_handlers(F.CODE_BASE, peek_base=peek_base)
    trimmed = F.make_room(img)
    end = F.place(img, F.CODE_BASE, code, "the read handlers")
    fault_at = fend = None
    repointed = []
    if fault:
        fault_at = (end + 1) & ~1
        fend = F.place(img, fault_at, fault_handler(
            fault_at, dump_at=FAULT_DUMP, resume=fault_resume), "fault handler")
        for i in range(NUM_VECTORS):
            at = VECTOR_TABLE + i * 4
            if struct.unpack_from("<I", img, at - BASE)[0] == DEFAULT_HANDLER:
                struct.pack_into("<I", img, at - BASE, fault_at | 1)
                repointed.append(i)
        if not repointed:
            sys.exit("no vector points at the 0x%08X default handler; the "
                     "image is not what this expects" % DEFAULT_HANDLER)
        end = fend
    tramp = F.route(img, (PEEK_OPCODE, NIBBLE_OPCODE, F.WHO_OPCODE), F.CODE_BASE)
    F.stamp_version(img, FLAVOUR, REV)
    return dict(handler=(F.CODE_BASE, len(code)), trimmed=trimmed,
                used=end - F.CODE_BASE, tramp=tramp,
                fault=((fault_at, fend - fault_at) if fault else None),
                fault_resume=fault_resume, fault_vectors=len(repointed),
                peek_base=PEEK_BASE if peek_base is None else peek_base)


def add_arguments(ap):
    hide = argparse.SUPPRESS
    ap.add_argument("--fault-resume", action="store_true", help=hide)
    ap.add_argument("--peek-base", type=lambda x: int(x, 0), default=None,
                    metavar="ADDR", help=hide)


def build(img, args):
    rep = apply(img, peek_base=args.peek_base, fault_resume=args.fault_resume)
    pb = rep["peek_base"]
    print("handler   0x%08X  %d bytes" % rep["handler"])
    print("          opcode 0x%02X  three septets, window 0x%08X..0x%08X"
          % (PEEK_OPCODE, pb, pb | PEEK_SPAN))
    print("          opcode 0x%02X  eight nibbles, the full 32-bit address"
          % NIBBLE_OPCODE)
    print("          both need cat = 0x%02X; without it they still reply"
          % OPCODE_MAGIC)
    print("trampoline 0x%08X 4 bytes  (spends opcodes 0x%02X and 0x%02X)"
          % (rep["tramp"][0], F.SPENT[0], F.SPENT[1]))
    if rep["fault"]:
        print("fault     0x%08X  %d bytes, %d hang-forever vectors repointed "
              "at it" % (rep["fault"] + (rep["fault_vectors"],)))
        print("          writes its dump to 0x%08X, then %s"
              % (FAULT_DUMP, "skips the faulting instruction"
                 if rep["fault_resume"] else "resets"))
    F.report_banner(img)
    F.report_room(rep["trimmed"], rep["used"])


def peek_message_nibble(addr, seq=0, cat=OPCODE_MAGIC):
    """A read request addressed as eight nibbles, high first. -> raw SysEx.

    Any 32-bit address, flash or SRAM. Only this build answers it.
    """
    if addr & 3:
        sys.exit("0x%08X is not word-aligned; peek reads a 32-bit word." % addr)
    nibbles = [(addr >> sh) & 0xF for sh in (28, 24, 20, 16, 12, 8, 4, 0)]
    return bytes([0xF0, 0x00, 0x20, 0x6B, 0x07, 0x01, seq, cat, NIBBLE_OPCODE]
                 + nibbles + [0xF7])
