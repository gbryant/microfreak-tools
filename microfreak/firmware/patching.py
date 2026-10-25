#!/usr/bin/env python3
"""Patch stock firmware 5.0.0.2084 with code from a plugin.

    mfreak fw patch --list
    mfreak fw patch speech MicroFreak_Firmware_Update_5_0_0_2084.mff --out speech.mff
    mfreak fw flash speech.mff

Each patch comes from a plugin. --list shows the installed ones and the ones
you can add. Only stock 5.0.0.2084 is accepted, the .mff from the MicroFreak
page on arturia.com; `mfreak fw info` shows what a file is. Given a .mff, the result holds all five images with the main one
patched.

Writes a file only. To go back, flash the stock .mff.
"""
# The rest of this module is the library plugins build patches with: the
# identity check, where code can go, how an opcode is claimed, the boot-screen
# banner, and writing the result. Every address is in stock 5.0.0.2084's main
# image. The evidence for each is in microfreak-emu's docs/SPEECH.md and
# docs/PROTOCOL.md.
import argparse
import hashlib
import os
import re
import struct
import subprocess
import sys
import tempfile

from microfreak.firmware import image as I

BASE = 0x0800C000                # where the main image's code loads

# ---------------------------------------------------------------- identity
# The bytes identify the build, not info.json's version, which nothing checks.
KNOWN_PAYLOAD_SHA256 = ("54be2e0b695e2279602513e1a9ed39906408ba221a45e782b"
                        "61273f0ef55d15f")
KNOWN_PAYLOAD_SIZE = 473695
KNOWN_PAYLOAD_NAME = "fw 5.0.0.2084  (nanowave_main__fw5_0_0_2084__2023_05_30.bin)"


def payload_sha256(img):
    return hashlib.sha256(bytes(img)).hexdigest()


def assert_supported_image(img, force=False):
    """Refuse anything but stock 5.0.0.2084. -> the image's sha256.

    `force` turns the refusal into a warning, for patching another build
    after re-deriving every address.
    """
    got = payload_sha256(img)
    if got == KNOWN_PAYLOAD_SHA256:
        return got
    why = ["this is not stock firmware 5.0.0.2084.",
           "  expected  %s  %d bytes" % (KNOWN_PAYLOAD_SHA256, KNOWN_PAYLOAD_SIZE),
           "            %s" % KNOWN_PAYLOAD_NAME,
           "  got       %s  %d bytes" % (got, len(img))]
    line1 = banner(img) if len(img) == KNOWN_PAYLOAD_SIZE else None
    if line1:
        why += ["", "  It is already patched (%s). Patch the stock file." % line1]
    elif len(img) != KNOWN_PAYLOAD_SIZE:
        why += ["", "  The size differs, so it is a different build."]
    if force:
        print("WARNING: patching an UNSUPPORTED image (--force).", file=sys.stderr)
        print("         sha256 %s" % got, file=sys.stderr)
        return got
    sys.exit("refusing to patch: " + "\n".join(why))


def known_build(code):
    """-> True for stock 5.0.0.2084 or a build made from it by this tool."""
    return payload_sha256(code) == KNOWN_PAYLOAD_SHA256 or banner(code) is not None


def identify(code):
    """-> one line on what a main image's code is."""
    if payload_sha256(code) == KNOWN_PAYLOAD_SHA256:
        return "stock 5.0.0.2084, which `fw patch` accepts"
    line1 = banner(code)
    if line1:
        who, old = answers(code), identity(code)
        return "%s, patched from stock 5.0.0.2084%s" % (
            line1, "; it names itself %s, rev %d" % who if who else
            "; a Device Inquiry answers 5.0.%s, build %d" % old if old else "")
    return "not stock 5.0.0.2084, so `fw patch` does not accept it"


# ---------------------------------------------------------------- code space
# The image has no free flash. Patch code goes in the top CODE_ROOM bytes of
# the speech vocabulary region, and any word bank that reaches into them is
# cut back to its last whole word.
FLASH_DESC = 0x08078838          # word_banks_[5], {ptr, size} per bank
NUM_BANKS = 5
VOCAB_START = 0x08078860         # where bank 0 starts; the banks run to REGION_END
REGION_END = 0x0807B367          # exclusive: the end of bank 4
CODE_BASE = (REGION_END - 256) & ~3
CODE_ROOM = REGION_END - CODE_BASE
# Live objects from here to 0x0807C84C. Never write them.
LIVE_LO, LIVE_HI = 0x0807B367, 0x0807C84C


def word_banks(img):
    """-> [(ptr, size)] for the five speech word banks, from the table."""
    return [struct.unpack_from("<II", img, FLASH_DESC - BASE + 8 * i)
            for i in range(NUM_BANKS)]


def set_word_banks(img, descs):
    """Write five (ptr, size) entries into the word bank table."""
    for i, (ptr, size) in enumerate(descs):
        struct.pack_into("<II", img, FLASH_DESC - BASE + 8 * i, ptr, size)


def describe_words(img):
    """-> one line per bank: its words and seconds, the bank it repeats, or
    that it is empty."""
    from microfreak import lpc
    out = []
    banks = word_banks(img)
    for i, (ptr, size) in enumerate(banks):
        if not size:
            out.append("bank %d  empty" % (i + 1))
            continue
        if (ptr, size) in banks[:i]:
            out.append("bank %d  repeats bank %d" % (i + 1, banks.index((ptr, size)) + 1))
            continue
        try:
            frames, bounds = lpc.decode_bank(bytes(img[ptr - BASE:ptr - BASE + size]))
        except Exception:
            out.append("bank %d  %d bytes that do not decode" % (i + 1, size))
            continue
        n = len(bounds) - 1
        out.append("bank %d  %2d word%s %5.1f s  %5d bytes"
                   % (i + 1, n, "s" if n != 1 else " ", len(frames) / lpc.FPS, size))
    return out


def make_room(img, start=CODE_BASE):
    """Cut every bank that reaches `start` back to a whole word.

    Call it before writing any code, while the banks are intact.
    -> [(bank, bytes before, bytes after, words before, words after)]
    """
    from microfreak import lpc
    trimmed = []
    for i in range(NUM_BANKS):
        ptr, size = struct.unpack_from("<II", img, FLASH_DESC - BASE + i * 8)
        if not size or ptr + size <= start:
            continue
        avail = max(0, start - ptr)
        data = bytes(img[ptr - BASE:ptr - BASE + size])
        offs = lpc.word_byte_offsets(data)
        keep = max((o for o in offs if o <= avail), default=0)
        struct.pack_into("<II", img, FLASH_DESC - BASE + i * 8, ptr, keep)
        trimmed.append((i, size, keep, len(offs) - 1, offs.index(keep)))
    return trimmed


def place(img, at, code, what):
    """Write `code` at `at`, inside the reserved region. -> its end address."""
    end = at + len(code)
    if at < CODE_BASE or end > REGION_END:
        sys.exit("%s would occupy 0x%08X..0x%08X, outside the %d bytes "
                 "reserved at 0x%08X" % (what, at, end, CODE_ROOM, CODE_BASE))
    if end > LIVE_LO:
        sys.exit("%s would reach 0x%08X, into live data at 0x%08X"
                 % (what, end, LIVE_LO))
    img[at - BASE:end - BASE] = code
    return end


def report_room(trimmed, used):
    """Print how much of the region is used, and what was cut for it."""
    print("region    %d of %d bytes used, %d spare"
          % (used, CODE_ROOM, CODE_ROOM - used))
    for i, was, now, wb, wa in trimmed:
        print("bank_%d    truncated %d -> %d bytes, %d -> %d words"
              % (i, was, now, wb, wa))


# ---------------------------------------------------------------- opcodes
# A SysEx message F0 00 20 6B 07 01 <seq> <cat> <opcode> ... is dispatched by
# its opcode byte, by two tbh tables. An opcode is free only if it bails in
# both. The first table reaches only 0x0806AD1E, short of the code region, so
# a claimed opcode is pointed at a b.w written over two adjacent bail entries,
# TRAMP_INDEX and TRAMP_INDEX + 1. The opcodes those two entries belong to are
# spent: their tbh now jumps into the b.w.
TBH_TABLE = 0x0804AD20           # dispatcher 1: target = TBH_TABLE + 2*halfword
OPCODE_BIAS = 0x15
TBH_MAX_INDEX = 0x48
BAIL = 0x0804AE60                # dispatcher 1's epilogue, shared: never detour
DISPATCH2_TABLE = 0x08046FA0     # dispatcher 2
DISPATCH2_BIAS = 0x1C
DISPATCH2_MAX = 0x37
DISPATCH2_BAIL = 0x08047060
TRAMP_INDEX = 0x20
SPENT = (TRAMP_INDEX + OPCODE_BIAS, TRAMP_INDEX + 1 + OPCODE_BIAS)

# Entered with r3 = reply code and r4 = the reply buffer, it copies
# [r4+1..6] into the outgoing reply.
REPLY_BUILDER = 0x0804AE6E
REPLY_CODE = 0x0E
OPCODE_TAIL = 0x0804AF2E         # movs r3,#0xE ; b <reply>: bail with a reply
# The dispatcher never reads `cat`, wire byte 7. Patch handlers require this
# value there, so a message that only happens to carry the opcode does nothing.
OPCODE_MAGIC = 0x2A


def assert_opcode_free(img, claimed, spent=()):
    """Refuse unless every opcode claimed or spent bails in both dispatchers."""
    tables = ((TBH_TABLE, BAIL, OPCODE_BIAS, TBH_MAX_INDEX),
              (DISPATCH2_TABLE, DISPATCH2_BAIL, DISPATCH2_BIAS, DISPATCH2_MAX))
    for op in tuple(claimed) + tuple(spent):
        for base, bail, bias, bound in tables:
            i = op - bias
            if not 0 <= i <= bound:
                continue                      # out of this dispatcher's range
            hw = struct.unpack_from("<H", img, base - BASE + i * 2)[0]
            if hw != (bail - base) // 2:
                sys.exit("opcode 0x%02X is not free: the dispatcher at 0x%08X "
                         "sends index 0x%02X to 0x%08X, a real handler. Using "
                         "it would freeze the instrument. Pick one that bails "
                         "in both tables." % (op, base, i, base + 2 * hw))


def free_opcodes(img):
    """-> every opcode that bails in both dispatchers and can be claimed."""
    out = []
    for op in range(OPCODE_BIAS, OPCODE_BIAS + TBH_MAX_INDEX + 1):
        try:
            assert_opcode_free(img, (op,))
        except SystemExit:
            continue
        out.append(op)
    return out


def route(img, opcodes, target):
    """Send `opcodes` to the code at `target`. Its first instructions tell
    them apart by r2, which holds the opcode minus OPCODE_BIAS.

    -> (trampoline address, TRAMP_INDEX)
    """
    assert_opcode_free(img, opcodes, SPENT)
    idxs = [op - OPCODE_BIAS for op in opcodes]
    if len(set(idxs)) != len(idxs):
        sys.exit("an opcode is claimed twice: %s"
                 % ", ".join("0x%02X" % op for op in opcodes))
    for op, i in zip(opcodes, idxs):
        if not 0 <= i <= TBH_MAX_INDEX:
            sys.exit("opcode 0x%02X is outside the dispatcher's table" % op)
    tbl = TBH_TABLE - BASE
    bail_hw = (BAIL - TBH_TABLE) // 2
    for i in idxs + [TRAMP_INDEX, TRAMP_INDEX + 1]:
        hw = struct.unpack_from("<H", img, tbl + i * 2)[0]
        if hw != bail_hw:
            sys.exit("table index 0x%02X (opcode 0x%02X) does not bail; it "
                     "targets 0x%08X. Refusing to overwrite a handler."
                     % (i, i + OPCODE_BIAS, TBH_TABLE + 2 * hw))
    tramp_at = TBH_TABLE + 2 * TRAMP_INDEX
    img[tramp_at - BASE:tramp_at - BASE + 4] = bw(tramp_at, target)
    for i in idxs:
        struct.pack_into("<H", img, tbl + i * 2, TRAMP_INDEX)
    return tramp_at, TRAMP_INDEX


# ---------------------------------------------------------------- assembly
def assemble(src):
    """-> the .text bytes of Thumb-2 assembly, via clang and objdump."""
    with tempfile.TemporaryDirectory() as td:
        s = os.path.join(td, "a.s")
        o = os.path.join(td, "a.o")
        open(s, "w").write(".syntax unified\n.thumb\n" + src + "\n")
        p = subprocess.run(["clang", "-target", "thumbv7em-none-eabi", "-c", "-o", o, s],
                           capture_output=True, text=True)
        if p.returncode != 0:
            sys.exit("assembly failed:\n%s" % p.stderr)
        q = subprocess.run(["objdump", "-s", "-j", ".text",
                            "--triple=thumbv7em-none-eabi", o],
                           capture_output=True, text=True)
        if q.returncode != 0:
            sys.exit("objdump failed:\n%s" % q.stderr)
    out = bytearray()
    for line in q.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or not all(c in "0123456789abcdef" for c in parts[0]):
            continue
        for chunk in parts[1:]:
            if all(c in "0123456789abcdef" for c in chunk) and len(chunk) % 2 == 0:
                out += bytes.fromhex(chunk)
            else:
                break
    return bytes(out)


def bw(pc, target):
    """Thumb-2 B.W (T4) at `pc`."""
    off = target - (pc + 4)
    if not (-(1 << 24) <= off < (1 << 24)) or off % 2:
        sys.exit("b.w out of range: 0x%08X -> 0x%08X" % (pc, target))
    imm = off >> 1
    s = (imm >> 23) & 1
    i1, i2 = (imm >> 22) & 1, (imm >> 21) & 1
    hw1 = 0xF000 | (s << 10) | ((imm >> 11) & 0x3FF)
    hw2 = 0x9000 | (((~i1 ^ s) & 1) << 13) | (((~i2 ^ s) & 1) << 11) | (imm & 0x7FF)
    return struct.pack("<HH", hw1, hw2)


def beqw(pc, target, cond=0):
    """Thumb-2 BEQ.W (T3) at `pc`, or another condition: 1 is BNE.W. T3
    carries J2 before J1 and has no inversion, so it is not `bw` with a
    condition."""
    off = target - (pc + 4)
    if not (-(1 << 20) <= off < (1 << 20)) or off % 2:
        sys.exit("b<cond>.w out of range: 0x%08X -> 0x%08X" % (pc, target))
    hw1 = (0xF000 | (((off >> 20) & 1) << 10)       # S
           | (cond << 6)                            # cond
           | ((off >> 12) & 0x3F))                  # imm6
    hw2 = (0x8000 | (((off >> 18) & 1) << 13)       # J1
           | (((off >> 19) & 1) << 11)              # J2
           | ((off >> 1) & 0x7FF))                  # imm11
    return struct.pack("<HH", hw1, hw2)


# ---------------------------------------------------------------- banner
# The boot screen shows this format string after the logo. A patch replaces
# line 1 with "PATCHED <flavour><rev>" and keeps Arturia's version as line 2.
VERSION_STR = 0x080757AC
VERSION_SLOT = 32                 # bytes, including the terminator
VERSION_ORIG = b"Firmware version\n%d.%d.%d.%d"
VERSION_TAIL = b"%d.%d.%d.%d"
DISPLAY_COLS = 16                 # the widest line 1 seen to fit


def version_banner(flavour, rev):
    """-> (format string bytes, line 1) for a patched build."""
    line1 = "PATCHED %s%d" % (flavour, rev)
    return line1.encode() + b"\n" + VERSION_TAIL, line1


def stamp_version(img, flavour, rev):
    """Put the patch's name on the boot screen. -> (line 1, bytes used)."""
    off = VERSION_STR - BASE
    found = bytes(img[off:off + len(VERSION_ORIG)])
    if found != VERSION_ORIG:
        sys.exit("refusing to stamp the version: 0x%08X holds %r, not Arturia's "
                 "%r. Either this is a different build or the address moved."
                 % (VERSION_STR, found[:40], VERSION_ORIG))
    new, line1 = version_banner(flavour, rev)
    if len(new) + 1 > VERSION_SLOT:
        sys.exit("version banner %r needs %d bytes of a %d-byte slot. Shorten "
                 "the flavour." % (new, len(new) + 1, VERSION_SLOT))
    if len(line1) > DISPLAY_COLS:
        print("warning: banner line 1 %r is %d characters; only %d are known "
              "to fit the display." % (line1, len(line1), DISPLAY_COLS),
              file=sys.stderr)
    img[off:off + VERSION_SLOT] = new + b"\0" * (VERSION_SLOT - len(new))
    return line1, len(new) + 1


def banner(img):
    """-> line 1 of a patched image's boot screen, or None if not patched."""
    off = VERSION_STR - BASE
    text = bytes(img[off:off + VERSION_SLOT]).split(b"\0")[0]
    if not text.startswith(b"PATCHED "):
        return None
    return text.split(b"\n")[0].decode("ascii", "replace")


def report_banner(img):
    off = VERSION_STR - BASE
    used = bytes(img[off:off + VERSION_SLOT]).index(b"\0") + 1
    print("banner    0x%08X  %d of %d bytes   the screen now reads:"
          % (VERSION_STR, used, VERSION_SLOT))
    print("            %s" % banner(img))
    print("            <Arturia's own version, unchanged>")


# ---------------------------------------------------------------- identity
# A patched build answers a MIDI Device Inquiry exactly as stock does,
#
#     F0 7E 7F 06 02  00 20 6B  06 00  06 01  05 00 00 24  F7
#
# so MIDI Control Center treats it as 5.0.0.2084. It names itself to mfreak
# by answering WHO_OPCODE, which stock firmware ignores:
#
#     request  F0 00 20 6B 07 01 <seq> 2A 48 F7
#     reply    F0 <letter> <rev> <format> 00 00 <seq> 00 18 F7
#
# The handler loads who_word() with one movw and sends it through the same
# five-septet reply as peek.
WHO_OPCODE = 0x48
WHO_FORMAT = 1


def who_word(letter, rev):
    """-> the 16-bit word a build answers WHO_OPCODE with."""
    if not (len(letter) == 1 and letter.isascii() and letter.isalpha()):
        sys.exit("identity %r must be one letter, A-Z or a-z" % letter)
    if not 0 <= rev <= 0x7F:
        sys.exit("revision %d does not fit in 7 bits (0-127)" % rev)
    return ord(letter) | rev << 7 | WHO_FORMAT << 14


def who_handler(base, letter, rev):
    """-> a handler that answers WHO_OPCODE alone, for a build with no other
    opcodes. Entered from the dispatcher with r4 = reply buffer, r5 = message."""
    src = """
        ldrb  r0, [r5, #6]        // magic guard
        cmp   r0, #%#x
        bne   8f
        movw  r0, #%#x
        movs  r2, #5
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
    8:  nop                       //   -> b.w OPCODE_TAIL
        nop
    """ % (OPCODE_MAGIC, who_word(letter, rev), REPLY_CODE)
    code = bytearray(assemble(src))
    code[-8:-4] = bw(base + len(code) - 8, REPLY_BUILDER)
    code[-4:] = bw(base + len(code) - 4, OPCODE_TAIL)
    return bytes(code)


WHO_ROOM = 40                    # who_handler's size, checked when placed
# A build with no other code, such as stock with embedded words, keeps
# who_handler at the top of the region, and its banks end below it.
WHO_AT = (REGION_END - WHO_ROOM) & ~3


def install_who(img, letter, rev):
    """Give a build with no other opcodes the who_handler at WHO_AT, and put
    the Device Inquiry builder back to stock if an older build changed it."""
    off = IDENTITY_STORES - BASE
    if identity(img):
        img[off:off + 24] = STOCK_STORES
    code = who_handler(WHO_AT, letter, rev)
    if len(code) > WHO_ROOM:
        sys.exit("who_handler is %d bytes, more than WHO_ROOM" % len(code))
    place(img, WHO_AT, code, "the identity handler")
    if not _routed(img):
        route(img, (WHO_OPCODE,), WHO_AT)


def _routed(img):
    """True when WHO_OPCODE already reaches the trampoline."""
    tbl = TBH_TABLE - BASE
    return struct.unpack_from("<H", img, tbl + (WHO_OPCODE - OPCODE_BIAS) * 2)[0] == TRAMP_INDEX


def answers(img):
    """-> (letter, rev) a patched image answers WHO_OPCODE with, or None.
    Looks for the handler's movw in the code region."""
    rev = banner_rev(img)
    if rev is None or rev > 0x7F:
        return None
    region = bytes(img[CODE_BASE - BASE:REGION_END - BASE])
    for c in range(0x41, 0x7B):
        letter = chr(c)
        if letter.isalpha() and movw_r0(who_word(letter, rev)) in region:
            return letter, rev
    return None


def movw_r0(imm):
    """-> the 4 bytes of `movw r0, #imm`."""
    hw1 = 0xF240 | ((imm >> 11) & 1) << 10 | (imm >> 12) & 0xF
    hw2 = ((imm >> 8) & 7) << 12 | (imm & 0xFF)
    return struct.pack("<HH", hw1, hw2)


# Builds made before WHO_OPCODE rewrote the six byte stores that end the
# Device Inquiry builder at 0x080239A8 to answer 05 00 <letter> <rev>.
# identity() still reads those images.
IDENTITY_STORES = 0x080239A8
STOCK_STORES = bytes.fromhex(
    "8df80e30"      # strb.w r3, [sp, #0xe]   patch = 0
    "8df805e0"      # strb.w lr, [sp, #5]
    "8df80670"      # strb.w r7, [sp, #6]
    "8df80760"      # strb.w r6, [sp, #7]
    "8df80c50"      # strb.w r5, [sp, #0xc]
    "8df80f40")     # strb.w r4, [sp, #0xf]   build = 0x24
STOCK_BUILD = 0x24


def _strb(rt, imm):
    return 0x7000 | imm << 6 | 1 << 3 | rt        # strb rt, [r1, #imm]


def identity_stores(letter, rev):
    """-> the 24 bytes an older build put there to answer 05 00 <letter> <rev>."""
    code = [_strb(7, 6), _strb(6, 7), _strb(5, 0xC),
            0x2400 | rev, _strb(4, 0xF),              # movs r4, #rev
            0x2700 | ord(letter), _strb(7, 0xE),      # movs r7, #letter
            0xBF00, 0xBF00, 0xBF00]
    return STOCK_STORES[4:8] + b"".join(h.to_bytes(2, "little") for h in code)


def identity(img):
    """-> (letter, rev) an older patched image answers a Device Inquiry
    with, or None if it answers as stock."""
    off = IDENTITY_STORES - BASE
    got = bytes(img[off:off + 24])
    letter, rev = chr(got[14]), got[10]
    if letter.isalpha() and got == identity_stores(letter, rev):
        return letter, rev
    return None


def banner_rev(img):
    """-> the revision on a patched image's boot screen, or None."""
    m = re.search(r"(\d+)$", banner(img) or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- output
def write(img, out, source=None, bin_path=None):
    """Write the patched code as `out`, a .mff or a packaged .bin.

    A .mff made from a .mff `source` carries its other images unchanged.
    """
    blob = I.package(img, 0)
    if I.looks_packaged(blob) is None or blob[I.HEADER:-1] != bytes(img):
        sys.exit("packaging did not round-trip; refusing to write %s" % out)
    if bin_path:
        open(bin_path, "wb").write(blob)
        print("wrote %s  (the main image, %d bytes)" % (bin_path, len(blob)))
    if not out.endswith(".mff"):
        open(out, "wb").write(blob)
        print("wrote %s  (the main image, %d bytes; `mfreak fw repack` makes "
              "it a .mff)" % (out, len(blob)))
        return
    name = os.path.basename(out).replace(".mff", ".bin")
    line1 = banner(img) or "custom"
    others = []
    if source and open(source, "rb").read(2) == b"PK":
        others = [r for r in I.read(source)[1] if r[0] != 0]
    I.write(out, [(0, name, blob)] + others,
            version="5.0.0.2084+" + line1.replace("PATCHED ", ""))
    got = I.read(out)[1]
    bad = [f for n, f, b in got
           if n != I.WAVETABLES and I.looks_packaged(b) is None]
    if bad or got[0][2] != blob:
        sys.exit("%s did not read back: %s" % (out, ", ".join(bad) or name))
    print("wrote %s  (%d image%s: %s; checksums match)"
          % (out, len(got), "" if len(got) == 1 else "s",
             ", ".join(I.NAMES.get(n, str(n)) for n, _, _ in got)))
    if not others:
        print("it holds the main image only, because %s is not a .mff"
              % os.path.basename(source or out))
    print("flash it with:")
    print("    mfreak fw flash %s" % out)


# ---------------------------------------------------------------- fw patch
def list_patches(found):
    from microfreak import plugin
    if found:
        print("installed:")
        for name, p in sorted(found.items()):
            print("  %-10s %s" % (name, p.firmware.help))
    missing = plugin.not_installed()
    if missing:
        print("not installed:")
        for name, (package, text) in sorted(missing.items()):
            print("  %-10s %s" % (name, text))
        print("\n`mfreak plugins` shows how to add one.")
    elif not found:
        print("no patches are installed")


def main():
    from microfreak import plugin
    found = plugin.firmwares()
    argv = sys.argv[1:]
    if argv and not argv[0].startswith("-") and argv[0] not in found:
        hint = plugin.missing(argv[0])
        if hint:
            print("mfreak: %s" % hint, file=sys.stderr)
            return 2
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="list the patches, installed or not")
    sub = ap.add_subparsers(dest="name", metavar="PATCH")
    hide = argparse.SUPPRESS
    for name, p in sorted(found.items()):
        mod = p.firmware.load()
        q = sub.add_parser(
            name, help=p.firmware.help,
            description=(mod.__doc__ or p.firmware.help).strip(),
            formatter_class=argparse.RawDescriptionHelpFormatter)
        q.add_argument("image", metavar="FILE",
                       help="the stock 5.0.0.2084 firmware: the .mff as "
                            "downloaded, or its main image")
        q.add_argument("--out", required=True, metavar="OUT.mff",
                       help="the firmware file to write")
        q.add_argument("--bin", metavar="PATH", help=hide)
        q.add_argument("--force", action="store_true", help=hide)
        if hasattr(mod, "add_arguments"):
            mod.add_arguments(q)
        q.set_defaults(build=mod.build, identity=p.firmware.identity)
    a = ap.parse_args(argv)
    if a.list or not a.name:
        list_patches(found)
        return 0
    img = I.main_code(a.image)
    assert_supported_image(img, a.force)
    a.build(img, a)
    if a.identity is not None:
        who = answers(img)
        if who != (a.identity, banner_rev(img)):
            sys.exit("the %s build does not answer opcode 0x%02X as %s"
                     % (a.name, WHO_OPCODE, a.identity))
        print("identity  opcode 0x%02X answers %s, rev %d" % ((WHO_OPCODE,) + who))
    write(img, a.out, source=a.image, bin_path=a.bin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
