#!/usr/bin/env python3
"""Load speech vocabulary over SysEx at runtime, by writing the DECODED frames.

    mfreak fw patch speech stock.mff --out speech.mff    # one-time patch
    mfreak speech upload table --bank w1.wav w2.wav --out v.bin  # build an upload
    mfreak speech upload dump   v.bin                   # show what it sends
    mfreak speech upload send   v.bin [--port MicroFreak]

``speech embed`` replaces vocabulary in flash, which needs a reflash each time.
This instead patches the firmware **once** to add a bounded RAM-write SysEx
command, after which auditioning vocabulary is a MIDI upload.

## The design: overwrite frames_, not the descriptor table

The firmware already decodes a word bank into RAM. `LPCSpeechSynthWordBank`
allocates `kLPCSpeechSynthMaxFrames (1024) x 14-byte Frame` = 14,336 bytes and
expands the selected bank's bitstream into it. **So the vocabulary is already in
RAM, already decoded** -- and in an easier form than the bitstream.

    0x20004880   frames_[1024]    14 bytes per Frame, the live decoded bank

That address is a link-time constant: it is Speech's first allocation from the
shared engine pool, so it equals the pool base (`audio_obj+0x50`). The pool is
handed out by a BufferAllocator built on the stack in `FUN_0800D49C`, reset
before each engine's Init, so Speech always gets the base. See microfreak-emu/docs/SPEECH.md.

The bank load is **lazy, inside Render, and guarded**:

    0x0805E646  ldr r3,[r5,#0x8]   ; loaded_bank_
    0x0805E648  cmp r11,r3
    0x0805E64A  beq 0x0805E654     ; already loaded -> render straight from frames_

So once a bank is loaded the firmware stops touching `frames_`, and an upload
into it survives every later render. **This needs no descriptor relocation, no
free RAM, and no boot-time stub at all** -- the factory bank loads itself and we
overwrite the result. An earlier version of this tool relocated the descriptor
table into `.bss` at `0x2000C000` and seeded it from an `init_stub` at boot; that
address was inside `.bss` and the stub never ran. Both are gone. See
microfreak-emu/docs/SPEECH.md, "The memory hunt was mostly unnecessary".

**The trade: this is volatile.** Changing the requested bank, or switching engine
away and back (vtable slot 1 is `WordBank::Reset`, which sets
`loaded_bank_ = -1`), reloads from flash and discards the upload. Upload, then
play. Permanent vocabulary is ``speech embed``'s job.

## Word boundaries are inherited, which is why only one address is needed

`word_boundaries_[32]` lives at `bank+0x14` and `num_words` at `bank+0x10`, but
the bank object's address is runtime-dependent. So this tool does **not** write
them: it lays each custom word into the frame span the factory word already
occupies, padding short words with silent frames. Custom word *k* therefore
replaces factory word *k* and inherits its length budget. `bank_4` gives 22 words
over 926 frames, ~42 frames a word against ~10 measured, so the budget is loose.

## The one firmware patch

`vocab_handler` claims SysEx opcode `OPCODE` (0x45 since v16), guarded by the
magic byte `OPCODE_MAGIC` in wire byte 6, which the dispatcher itself never
reads. It is a bounded write: offset, count, then a 7-to-8 packed payload
(see `CHUNK`), refusing anything past `frames_`.

0x45 was chosen because it bails in BOTH dispatcher tables,
which `assert_opcode_free` now checks at build time -- see the note at
`OPCODE`, and `two-sysex-opcode-dispatchers` for why one table is not enough. It falls into the original handler's tail at
`0x0804AF2E`, so the device's reply is unchanged.

**Where the 58 bytes of code live.** The five factory banks fill
`0x08078860`..`0x0807B367` exactly, and the image has no erased run anywhere, so
there is no free flash to put it in. The handler therefore comes out of the
**vocabulary budget**: `CODE_ROOM` bytes at the TOP of the region
(`microfreak.firmware.patching`), with the banks ending at `CODE_BASE` and any overlapping bank truncated to
a clean word boundary. Cost ~58 bytes of the 11,015, against a whole bank's
worth of words for any other placement. The owner's reasoning, 2026-10-05: with
custom vocabulary the built-in words are irrelevant, so budget is the right
thing to spend.

Do NOT put code in `0x0807B367`..`0x0807C84C`. That was once recorded as
"unreferenced slack"; it is seven live objects, one of them read by
`0x0805ECCA`.

Nibble encoding is used rather than the firmware's 7-to-8 packing because SysEx
is 7-bit and the unpacker is then three instructions instead of a loop carrying
state. It doubles transfer size, which is not the constraint.
"""

import argparse, os, struct, sys

import microfreak.lpc as S
import mfreak_speech.vocab as vocab
from microfreak.firmware import patching as F
from microfreak.firmware.patching import (
    BASE, FLASH_DESC, NUM_BANKS, OPCODE_BIAS, OPCODE_MAGIC, OPCODE_TAIL,
    REPLY_BUILDER, REPLY_CODE, assemble as _assemble, beqw, bw)
from microfreak.firmware.peek import (INQUIRY_REPLY_PREFIX, INQUIRY_REQUEST,
                                      PEEK_BASE, PEEK_OPCODE, peek_message,
                                      peek_reply)

FRAMES_BASE = 0x20004880         # frames_, Speech's first pool allocation
FRAMES_SIZE = 0x3800             # 1024 frames x 14 bytes

# Four voices, each with its own Speech engine, bank object and frames_ buffer.
# Addresses came from hooking the `str.w r4,[r0,#0x94]` in WordBank::Init; the
# stride is uniform, and voice 0's bank fields were then read back live
# (word_banks_ = 0x08078838, num_banks = 5) to confirm they are real objects.
# A single note loads voice 0 only, but a chord spreads, so writes fan out.
BANK_BASE = 0x200093AC           # voice 0's LPCSpeechSynthWordBank
VOICE_STRIDE = 0x59E4

# loaded_bank_, at bank+0x08.  It sits BELOW the writable window's +0x0C floor,
# so the host cannot set it -- which is why, before 2026-10-06, an upload
# reached all four voices' frames_ but left voices that had never rendered at
# `loaded_bank_ = -1`.  Their first render then saw -1 != requested, reloaded
# the bank from flash and decoded factory vocabulary straight over our frames:
# paraphonic chords spoke factory words on every voice but the one that had
# already played.  The handler now copies voice 0's value to the rest, which
# needs no new parameter because voice 0 is the voice the user just played.
LOADED_BANK_OFF = 0x08
NUM_VOICES = 4

# The writable window inside the bank object: +0x0C frame count, +0x10
# num_words, +0x14.. word_boundaries_.  It deliberately STOPS at +0x94, which
# is the frames_ pointer -- the handler physically cannot corrupt it.
# v3, 2026-10-06: the window floor drops from +0x0C to +0x00 so the host can
# reach word_banks_ (+0x00) and num_banks (+0x04).  Read out of the firmware:
#
#   WordBank::Init  str r2,[r0,#0x4]   num_banks    = 5
#                   str r1,[r0]        word_banks_  = the flash table
#   WordBank::Reset touches ONLY +0x08, +0x0C, +0x10 and the boundary array.
#
# So +0x00 and +0x04 SURVIVE Reset, and the Load guard is
#
#   ldr r3,[r5,#0x8] ; cmp r11,r3 ; beq render     already loaded
#   ldr r3,[r5,#0x4] ; cmp r11,r3 ; blt load       load ONLY IF bank < num_banks
#                                   (else) render
#
# num_banks = 0 therefore makes the firmware never reload a bank again, so an
# upload survives voice wake, bank switch and engine Reset.  Init restores 5, so
# a power cycle or startup brings factory behaviour back by itself.
BANK_WINDOW_BASE = 0x00                  # the writable window starts here
BANK_FIELD_OFF = 0x0C                    # frame count, num_words, boundaries
WORD_BANKS_OFF = 0x00                    # the bank descriptor table pointer
NUM_BANKS_OFF = 0x04                     # how many banks Load will consider
BANK_WINDOW = 0x94 - BANK_WINDOW_BASE    # 0x94: still stops short of frames_

REGION_FRAMES = 0                # buf[10] bit 6 clear
REGION_BANK = 0x40               # buf[10] bit 6 set; count is bits 0..5
# v16: 0x45, not 0x40. 0x40 was chosen in v12 because it "bails mid-dead-zone"
# in the table at 0x0804AD20 -- and it has a real handler at 0x0804716A in the
# second dispatcher, which is the freeze. 0x45 bails in BOTH tables, as
# `assert_opcode_free` now checks at build time rather than by argument.
OPCODE = 0x45

# Payload bytes per message.
#
# The firmware's SysEx receive buffer gives us 36 bytes of payload space
# (buf[11..46]) -- measured: with the old two-nibbles-per-byte encoding, 18 data
# bytes landed byte-perfectly and 19+ did not, and at CHUNK=48 the bad offsets
# were exactly {16,20,...,44} mod 48.
#
# 7-to-8 packing carries 7 data bytes per 8 wire bytes (one byte of MSBs, then
# seven 7-bit bytes), so wire(C) = ceil(C/7) + C.  C=31 needs exactly 36 and is
# the largest that fits -- 1.72x the nibble encoding, which is why the handler
# grew an unpacker.
CHUNK = 31

# Wire time per message at 31250 baud.  Spacing must exceed it, but that is not
# sufficient on its own: message loss is also rate-dependent above ~80 ms, and
# 250 ms is the spacing verified byte-for-byte.
WIRE_MS_PER_MESSAGE = (9 + 3 + (CHUNK + (CHUNK + 6) // 7) + 1) * 10 * 1000.0 / 31250.0

INIT_PATCH = 0x0805B1A8          # movs r2,#5 ; ldr r1,[pc,#0x54]  -- 4 bytes
INIT_TAIL = 0x0805B1AC           # the bl we fall into
FLASH_TABLE = 0x08078838         # word_banks_ as shipped

# Magic and table live in frames_' unused tail.  .bss is zeroed at boot, so a
# cold start finds no magic and takes the flash branch -- factory vocabulary
# behaves normally until something uploads.
# A 16-BIT magic, not 32: the stub compares it with a single movw instead of
# movw+movt, which buys 4 bytes -- and the region had only 51 free with peek in
# it. 16 bits of specificity is still ample here, because .bss is zeroed at boot
# and nothing else reaches this address (bank_4, the largest factory bank,
# decodes to 0x32A4 and the magic sits at 0x37D4).
MAGIC = 0x4356464D               # 'MFVC' little-endian, as v5 had it
# v7 narrowed this to 0x0000464D to save 4 bytes in the init stub, leaving
# 15 significant bits. The stub reads it from `frames_ + MAGIC_OFF`, and
# WordBank::Init does NOT zero frames_ (Ghidra, 0x0805DFD0: it memsets only
# the 128 boundary bytes), so the check reads UNINITIALISED SRAM. A narrow
# sentinel can match by accident and install a table pointer at a table that
# was never written. Not the cause of any freeze -- v6 froze while still
# carrying the 32-bit value -- but restored because the risk is free to avoid.
# v5: magic and table sit at the very TOP of frames_, which recovers the 468
# bytes that used to sit dead above them.  Nothing else can reach up here: the
# largest factory bank, bank_4, decodes to 926 frames = 0x32A4, and our own
# per-bank cap is lower still.  Only a full 1024-frame bank would collide, and
# that would collide wherever the metadata lived.
MAGIC_OFF = 0x3800 - (4 + NUM_BANKS * 8)     # 0x37D4
TABLE_OFF = MAGIC_OFF + 4                    # 5 x {ptr,size} = 40 bytes
STREAM_OFF = 0x2000              # the bitstream region starts here
STREAM_MAX = MAGIC_OFF - STREAM_OFF          # 5,632 bytes
# Decoded frames must stop short of the bitstream, or a decode overwrites its
# own source.  A factory bank_4 load is 926 frames = 0x32A4, which already
# exceeds this -- that is fine only because once the magic is set NO factory
# load happens.  Upload the stream AFTER any factory load, never before.
MAX_FRAMES_WITH_TABLE = STREAM_OFF // S.FRAME_BYTES      # with the old fixed layout
# word_boundaries_[32] sits at bank+0x14 and 0x14 + 32*4 = 0x94, which is exactly
# the frames_ pointer.  A 33rd word in a bank makes the FIRMWARE's own decode
# overwrite it, so this is an overflow boundary, not a soft limit.
MAX_WORDS_PER_BANK = S.MAX_WORDS


def vocab_handler(base, peek=None):
    """A bounded write, fanned out to all four voices -- as v5 has it.

    v14: `peek` is the address of `peek_handler`, and claims a SECOND opcode.
    The dispatch sits after the magic guard, so one guard covers both opcodes
    and an unmagicked peek touches nothing either. `peek=None` emits the
    handler alone, exactly as v13 had it.

    Two-pass, because the dispatch's target is laid out after this code: call
    once with any in-range `peek` to measure the length -- the encoding is
    fixed-width, so the length does not depend on the target -- then again with
    the real address once the layout is known. `apply_patch` does that.

    v8 removed the fan-out on the static-analysis finding that only ONE Speech
    engine and one WordBank exist (0x080359B0 has a single caller, from
    .init_array), so k=1,2,3 write unrelated memory. That reasoning still holds,
    and those three writes are pointless. But they are also HARMLESS, proven
    both ways on hardware 2026-10-09: v5 fans out and works, v8 removed it and
    still froze. An upload has never been confirmed to work WITHOUT the fan-out,
    because every version that lacked it also froze for another reason.

    So it stays. v10 changes exactly one thing from v5 -- the dispatch -- and
    removing the fan-out would be a second, unvalidated change. Drop it later as
    its own step, with its own hardware test.

    v8: the fan-out is GONE. It wrote BANK_BASE + k*VOICE_STRIDE for k=0..3 on
    the belief that there were four voice objects. Static analysis 2026-10-08
    disproves that: the constructor installing the seven engine vptrs
    (0x080359B0) is called EXACTLY ONCE, at 0x08037CC2, on a single ~94 KB
    object -- so there is one set of engine objects, one Speech engine, and one
    LPCSpeechSynthWordBank. Corroborating: 0x59E4 appears nowhere in the image,
    and four voices at that stride from 0x20008EF4 would end past _ebss
    (0x2001F230) by 0x454 bytes, which cannot be.

    So k=1,2,3 were writing into unrelated memory. That is the v6 upload freeze,
    v2's inert propagation, and every paraphonic failure: three bad writes per
    message. Removing them is a fix, not a limitation -- all voices share the
    one bank, so a single write reaches all of them.

    v2 copied voice 0's `loaded_bank_` to voices 1-3 here.  That was INERT --
    a voice Resets when brought into service, which is always after an upload,
    so the value was overwritten before it mattered.  Removed in v5; the v4
    init stub is the fix that actually survives.

    Message layout after the 9-byte header:
        buf[8..9]  destination offset, 14 bits, big-endian 7+7
        buf[10]    bit 6 = region (0 frames_, 0x40 bank fields), bits 0..5 count
        buf[11..]  payload, 7-to-8 packed: per group of 7 data bytes, one
                   byte of their high bits (LSB-first, bit j = byte j) then
                   the seven 7-bit bodies. See `pack78`.

    Each region is bounded, and the bank window stops at +0x94 so the handler
    cannot overwrite the frames_ pointer.  r4 and r7 are pushed because the
    reply path at 0x0804AE6E only reloads r7, r6, r5 and r0-r2, so r4 and the
    high registers must be restored.  The count is re-read from the buffer per
    voice rather than kept live across voices.

    Why 7-to-8 and not a nibble per byte: 36 bytes of payload space carry 31
    data bytes instead of 18.  The MSB byte is consumed with `lsrs` so its
    bit 0 applies to the first body byte, which is why `pack78` writes the
    MSBs LSB-first.  (The layout above described nibbles until 2026-10-18 --
    stale from an earlier scheme, and flatly contradicted by this paragraph
    four lines below it.  The code has always been 7-to-8.)
    """
    src = """
        push  {r4, r7, r8, r9}
        ldrb  r0, [r5, #6]        // cat: the magic, read by nothing else
        cmp   r0, #%#x
        bne   9f                  // absent -> pop and reply, touch nothing
        cmp   r2, #%#x            // v14: still the biased opcode here
        nop                       //   -> bne.w peek_handler: not an upload
        nop
        ldrb  r0, [r5, #8]
        ldrb  r1, [r5, #9]
        lsls  r0, r0, #7
        orrs  r0, r0, r1          // r0 = destination offset
        ldrb  r2, [r5, #10]
        and   r7, r2, #%#x        // region flag
        and   r2, r2, #0x3f       // byte count
        adds  r1, r0, r2
        cmp   r7, #0
        beq   3f
        cmp.w r1, #%#x            // bank window: stops short of frames_
        bhi   9f
        movw  r3, #%#x
        movt  r3, #%#x
        b     4f
    3:  cmp.w r1, #%#x            // frames_ window
        bhi   9f
        movw  r3, #%#x
        movt  r3, #%#x
    4:  adds  r3, r3, r0          // voice 0 destination
        movs  r7, #%d             // voices remaining
    5:  add.w r8, r5, #11         // packed payload
        ldrb  r9, [r5, #10]
        and   r9, r9, #0x3f       // count, re-read per voice
        mov   r4, r3              // working destination
    6:  cmp   r9, #0              // next group of seven
        beq   7f
        ldrb  r2, [r8], #1        // the group's MSB byte
        movs  r1, #7
    8:  cmp   r9, #0
        beq   7f
        cmp   r1, #0
        beq   6b                  // group exhausted, fetch the next MSB byte
        ldrb  r0, [r8], #1
        lsrs  r2, r2, #1          // bit 0 -> carry, so MSBs are LSB-first
        bcc   1f
        orr   r0, r0, #0x80
    1:  strb  r0, [r4, #0]
        adds  r4, r4, #1
        subs  r9, r9, #1
        subs  r1, r1, #1
        b     8b
    7:  movw  r1, #%#x            // next voice (too big for addw's 12 bits)
        add   r3, r3, r1
        subs  r7, r7, #1
        bne   5b
    9:  pop   {r4, r7, r8, r9}
        nop
        nop
    """ % (OPCODE_MAGIC, OPCODE - OPCODE_BIAS, REGION_BANK,
           BANK_WINDOW, (BANK_BASE + BANK_WINDOW_BASE) & 0xFFFF,
           (BANK_BASE + BANK_WINDOW_BASE) >> 16,
           FRAMES_SIZE, FRAMES_BASE & 0xFFFF, FRAMES_BASE >> 16,
           NUM_VOICES, VOICE_STRIDE)
    code = bytearray(_assemble(src))
    code[-4:] = bw(base + len(code) - 4, OPCODE_TAIL)

    # Locate the dispatch by its assembled bytes rather than a fixed index, so
    # a change to the guard above it cannot silently patch the wrong place. The
    # pattern must appear exactly once.
    at = code.find(PEEK_DISPATCH)
    if at < 0 or code.find(PEEK_DISPATCH, at + 1) >= 0:
        sys.exit("peek dispatch placeholder appears %d times, expected 1"
                 % (0 if at < 0 else 2))
    if peek is None:
        del code[at:at + len(PEEK_DISPATCH)]       # v13: no second opcode
    else:
        code[at + 2:at + 6] = beqw(base + at + 2, peek, cond=1)   # bne.w
    return bytes(code)


def init_stub(base):
    """Pick word_banks_ for WordBank::Init: our RAM table, or Arturia's.

    Falls into INIT_TAIL so Init runs normally either way.  Only r1, r2 and r12
    are touched -- r0 and r3 already hold Init's other arguments.
    """
    magic_at = FRAMES_BASE + MAGIC_OFF
    table_at = FRAMES_BASE + TABLE_OFF
    src = """
        movw  r12, #%#x
        movt  r12, #%#x
        ldr.w r1, [r12]
        movw  r2, #%#x
        movt  r2, #%#x
        cmp   r1, r2
        bne   1f
        add.w r1, r12, #%d        // magic present -> our table
        b     2f
    1:  movw  r1, #%#x            // absent -> the flash table, factory behaviour
        movt  r1, #%#x
    2:  movs  r2, #%d             // num_banks, as the original did
        nop
        nop
    """ % (magic_at & 0xFFFF, magic_at >> 16,
           MAGIC & 0xFFFF, MAGIC >> 16,
           TABLE_OFF - MAGIC_OFF,
           FLASH_TABLE & 0xFFFF, FLASH_TABLE >> 16,
           NUM_BANKS)
    code = bytearray(_assemble(src))
    code[-4:] = bw(base + len(code) - 4, INIT_TAIL)
    return bytes(code)


def peek_handler(base, peek_base=None):
    """Read 4 bytes into the reply's header slots, from `peek_base` + offset.

    `peek_base` is baked in as the `orr` immediate, so **it is a property of
    the firmware, not of the host.** The three septets give a 21-bit offset and
    the instruction is an `orr`, so the reachable window is exactly
    `peek_base | 0..0x1FFFFF` -- a 21-bit offset cannot set bit 27, which is
    why the default SRAM build cannot see flash at 0x08000000 however the host
    addresses it. Build with `--peek-base 0x08000000` to read internal flash
    instead, which covers the bootloader at 0x08000000-0x0800BFFF and the
    512-byte record at 0x08008000 that marks a DFU request.

    One build reads one window. Changing it host-side alone would send offsets
    the device ORs onto its own base and return a different address's word
    without complaining, so `peek_message` takes the same value and the two are
    checked against each other rather than assumed equal.

    r4 = reply buffer and r5 = the message, both still live at the bail.
    Deliberately does NOT clear [r4+6]; that byte carries whatever was there
    and the host only reads five septets. Those 2 bytes matter -- the region has
    single-digit slack.

    v14: reached from `vocab_handler`'s entry dispatch, which means
    `vocab_handler`'s prologue has already pushed {r4,r7,r8,r9}. This tails into
    the reply builder instead of returning through that epilogue, so it has to
    undo the push itself -- otherwise the reply builder returns on a stack that
    is four registers deep, which is a freeze, not a bad read.
    """
    src = """
        cmp   r2, #%#x            // WHO_OPCODE, biased; else a read
        bne   3f
        movw  r0, #%#x
        b     5f
    3:  ldrb  r0, [r5, #8]
        ldrb  r1, [r5, #9]
        ldrb  r2, [r5, #10]
        orr   r0, r0, r1, lsl #7
        orr   r0, r0, r2, lsl #14
        orr   r0, r0, #%#x
        ldr   r0, [r0]
    5:  movs  r2, #5
        add   r1, r4, #1
    1:  and   r3, r0, #0x7f
        strb  r3, [r1]
        adds  r1, r1, #1
        lsrs  r0, r0, #7
        subs  r2, r2, #1
        bne   1b
        movs  r3, #%#x
        pop   {r4, r7, r8, r9}    // v14: undo vocab_handler's prologue
        nop
        nop
    """ % (F.WHO_OPCODE - OPCODE_BIAS, F.who_word("S", PATCH_REV),
           peek_base if peek_base is not None else PEEK_BASE, REPLY_CODE)
    code = bytearray(_assemble(src))
    code[-4:] = bw(base + len(code) - 4, REPLY_BUILDER)
    return bytes(code)


# The 6 bytes a second opcode costs at the handler entry. `tbh [pc, r2, lsl #1]`
# leaves r2 holding the biased opcode and the table trampoline is a plain b.w,
# so r2 is still the opcode when our code starts -- which is what makes this
# cheap, and what made the old `bail_trampoline` unsound: at the shared epilogue
# r2 is whatever the finished handler left.
# `cmp r2,#imm8` is T1, 0x2A00 | imm8, then two 16-bit nops as the placeholder
# the real beq.w overwrites. Derived rather than spelled out so it follows
# PEEK_OPCODE.
PEEK_DISPATCH = bytes([(OPCODE - OPCODE_BIAS) & 0xFF, 0x2A,
                       0x00, 0xBF, 0x00, 0xBF])


def apply_patch(img, peek_base=None):
    """Patch `img` in place. -> a report dict.

    Three pieces go at the top of the vocabulary region, in order: the upload
    handler, the init stub, and the memory read. The handler's entry
    dispatches to the read, whose address is only known once the stub is
    placed, so the handler is built twice and must come out the same length.
    """
    code = vocab_handler(F.CODE_BASE, peek=F.CODE_BASE)
    trimmed = F.make_room(img)
    end = F.place(img, F.CODE_BASE, code, "the upload handler")

    istub_base = (end + 1) & ~1                  # keep it halfword aligned
    istub = init_stub(istub_base)
    iend = F.place(img, istub_base, istub, "the init stub")
    img[INIT_PATCH - BASE:INIT_PATCH - BASE + 4] = bw(INIT_PATCH, istub_base)

    peek_at = (iend + 1) & ~1
    peek = peek_handler(peek_at, peek_base=peek_base)
    pend = F.place(img, peek_at, peek, "the memory read")
    code2 = vocab_handler(F.CODE_BASE, peek=peek_at)
    if len(code2) != len(code):
        sys.exit("handler changed length between passes: %d then %d"
                 % (len(code), len(code2)))
    F.place(img, F.CODE_BASE, code2, "the upload handler")

    tramp = F.route(img, (OPCODE, PEEK_OPCODE, F.WHO_OPCODE), F.CODE_BASE)
    F.stamp_version(img, PATCH_FLAVOUR, PATCH_REV)
    return dict(handler=(F.CODE_BASE, len(code)), trimmed=trimmed,
                init_stub=(istub_base, len(istub)), init_patch=INIT_PATCH,
                peek=(peek_at, len(peek)),
                peek_base=(PEEK_BASE if peek_base is None else peek_base),
                used=pend - F.CODE_BASE, tramp=tramp)


PATCH_FLAVOUR = "speech"
PATCH_REV = 19



def factory_bounds(img, bank):
    """-> (frame bounds, frame count) of a factory bank, by decoding it."""
    ptr, size = struct.unpack_from("<II", img, FLASH_DESC - BASE + bank * 8)
    if not size:
        sys.exit("bank %d is empty in this image" % bank)
    frames, bounds = S.decode_bank(bytes(img[ptr - BASE:ptr - BASE + size]))
    return frames, bounds


def build_upload(frame_lists, bounds, bank, first=0):
    """-> [(offset, bytes)] writes into frames_, one per replaced word.

    Custom word k is laid into factory word (first+k)'s frame span and padded
    with silent frames, so no boundary table has to be written.  `first` exists
    because the Word knob picks the slot: factory preset 9 is the only one on
    bank 4 and its Word value selects slot 17, so replacing slot 0 would be
    inaudible through it."""
    slots = len(bounds) - 1
    if first + len(frame_lists) > slots:
        sys.exit("words %d..%d but bank %d has only %d slots"
                 % (first, first + len(frame_lists) - 1, bank, slots))
    silent = {"energy": 0, "period": 0, "k0": 0, "k1": 0,
              "k2": 0, "k3": 0, "k4": 0, "k5": 0, "k6": 0,
              "k7": 0, "k8": 0, "k9": 0}
    writes, notes = [], []
    frame_lists = [S.end_with_silence(S.match_device_range(S.trim_leading_silence(fr)))
                   for fr in frame_lists]
    for j, fr in enumerate(frame_lists):
        k = first + j
        room = bounds[k + 1] - bounds[k]
        kept = fr[:room] if len(fr) <= room else fr[:room - 1] + [silent]
        pad = room - len(kept)
        notes.append((k, len(fr), room, pad, len(fr) > room))
        blob = S.frames_to_bytes(list(kept) + [silent] * pad)
        writes.append((REGION_FRAMES, bounds[k] * S.FRAME_BYTES, blob))
    return writes, notes


def pack78(data):
    """-> 7-to-8 packed bytes: per group of 7, a byte of MSBs then the 7-bit bodies.

    The MSB byte carries byte j's high bit in bit j, LSB-first, because the
    handler consumes it with `lsrs r2,r2,#1` and tests the carry -- which reads
    bit 0 first.
    """
    out = bytearray()
    for i in range(0, len(data), 7):
        grp = data[i:i + 7]
        msb = 0
        for j, b in enumerate(grp):
            if b & 0x80:
                msb |= 1 << j
        out.append(msb)
        out += bytes(b & 0x7F for b in grp)
    return bytes(out)


def messages(writes):
    """-> SysEx byte strings, each writing at most CHUNK bytes.

    `writes` is [(region, offset, blob)].  CHUNK must stay <= 0x3F because the
    count now shares buf[10] with the region flag.
    """
    assert CHUNK <= 0x3F, "count is six bits now"
    out = []
    for region, base_off, blob in writes:
        limit = BANK_WINDOW if region == REGION_BANK else FRAMES_SIZE
        for off in range(0, len(blob), CHUNK):
            part = blob[off:off + CHUNK]
            dest = base_off + off
            if dest + len(part) > limit:
                sys.exit("write to +0x%X (%d bytes) passes the region limit 0x%X"
                         % (dest, len(part), limit))
            # F0 | 00 20 6B 07 | 01 | seq | cat | opcode | args ... | F7
            # The dispatcher reads buf[7] as the opcode and buf[5] as the
            # sequence number, so the opcode is the NINTH wire byte.
            body = bytearray([0xF0, 0x00, 0x20, 0x6B, 0x07, 0x01, 0x00,
                              OPCODE_MAGIC, OPCODE])
            body += bytes([(dest >> 7) & 0x7F, dest & 0x7F, region | len(part)])
            body += pack78(part)
            body.append(0xF7)
            out.append(bytes(body))
    return out


# Payload files carry the opcode they were built for.
#
# v6 moved the upload from 0x5A to a free opcode. A payload built for v1-v5
# still carries 0x5A, and on v6 that opcode is RESTORED TO FACTORY -- so
# sending a stale file would invoke the real sample-directory-clear handler and
# wipe the user's samples. Silent in the other direction (0x40 falls to the
# bail on older firmware, doing nothing).
#
# So the file is stamped and refuses to load against a mismatched build. An
# unstamped file is from before v6 and is rejected by name.
# Bumped to MFU2 when the handler gained its magic-argument guard. A payload
# built before that carries cat = 0x00, so a guarded build would bail on every
# message and do nothing at all -- a silent no-op, which is the worst failure
# mode to debug. The stamp makes it an error instead.
PAYLOAD_MAGIC = b"MFU2"


def pack_upload(writes, opcode=None):
    """-> a file holding the writes, so `send` and `dump` need no wav files."""
    out = bytearray(PAYLOAD_MAGIC + struct.pack("<BBH", opcode or OPCODE, 0, 0))
    out += struct.pack("<I", len(writes))
    for region, off, blob in writes:
        out += struct.pack("<III", region, off, len(blob)) + blob
    return bytes(out)


def unpack_upload(data):
    if data[:4] != PAYLOAD_MAGIC:
        if data[:4] == b"MFU1":
            sys.exit("this payload was built by an older version and would "
                     "do nothing. Rebuild it.")
        sys.exit("this payload was built by an older version and uses opcode "
                 "0x5A, which clears the sample directory. Rebuild it.")
    built_for = data[4]
    if built_for != OPCODE:
        sys.exit("payload was built for opcode 0x%02X but this build uses 0x%02X. "
                 "Rebuild it, or the upload will go somewhere unintended."
                 % (built_for, OPCODE))
    n, = struct.unpack_from("<I", data, 8)
    pos, writes = 12, []
    for _ in range(n):
        region, off, size = struct.unpack_from("<III", data, pos)
        pos += 12
        writes.append((region, off, data[pos:pos + size]))
        pos += size
    return writes


def build_sentence(frame_lists):
    """-> writes that install an ARBITRARY vocabulary, boundaries and all.

    Lays the words out contiguously from frame 0 and writes the bank's own
    bookkeeping alongside the frames, so word count and word lengths are free
    rather than inherited from whatever the factory bank happened to hold:

        +0x0C  total frame count
        +0x10  num_words
        +0x14  word_boundaries_[0 .. num_words]

    The boundary table needs num_words+1 entries -- the loader writes
    bounds[num_words] as the end of the last word -- which is why MAX_WORDS is
    31 and not 32.  No bitstream is involved, so the encoder is not in the
    loop; these are the decoded 14-byte frames the firmware would have
    produced itself.
    """
    words = [S.trim_leading_silence(fr) for fr in frame_lists]
    words = [S.end_with_silence(S.match_device_range(w)) for w in words if w]
    if len(words) > S.MAX_WORDS:
        sys.exit("%d words exceeds the safe maximum of %d (a 32nd boundary "
                 "entry would land on the frames_ pointer)"
                 % (len(words), S.MAX_WORDS))
    total = sum(len(w) for w in words)
    if total > S.MAX_FRAMES:
        sys.exit("%d frames exceeds kLPCSpeechSynthMaxFrames = %d (%.1f s at %g FPS)"
                 % (total, S.MAX_FRAMES, total / S.FPS, S.FPS))

    bounds, n = [], 0
    for w in words:
        bounds.append(n)
        n += len(w)
    bounds.append(n)

    frames = []
    for w in words:
        frames.extend(w)
    fields = struct.pack("<II", total, len(words)) + b"".join(
        struct.pack("<I", b) for b in bounds)
    if BANK_FIELD_OFF - BANK_WINDOW_BASE + len(fields) > BANK_WINDOW:
        sys.exit("bank fields are %d bytes, window is %d" % (len(fields), BANK_WINDOW))
    # Bank fields FIRST, deliberately.  Sent last, after the 29 frames messages
    # of a four-word upload, they do not land -- measured 2026-10-05, and sent
    # first the same message lands and then survives the whole frames burst.
    # The cause is not established (it resembles the message-loss seen at the
    # start of a too-fast burst), so this ordering is a workaround and wants
    # explaining before any hardware use.  The transient it creates is benign:
    # for a moment num_words describes the new vocabulary while frames_ still
    # holds the old, and uploads happen between notes anyway.
    return ([(REGION_BANK, BANK_FIELD_OFF - BANK_WINDOW_BASE, fields),
             (REGION_FRAMES, 0, S.frames_to_bytes(frames))],
            words, bounds, total)


def freeze_writes(num_banks=0):
    """Set num_banks, which the Load guard compares the requested bank against.

    0 means "never reload", so an uploaded frame table survives every Reset.
    5 restores factory behaviour without a power cycle.
    """
    return [(REGION_BANK, NUM_BANKS_OFF - BANK_WINDOW_BASE,
             struct.pack("<I", num_banks))]


def table_writes(addr):
    """Point word_banks_ at `addr`, a 5 x {ptr,size} table.

    Validated host-side because the firmware will dereference it: a bogus
    pointer makes Load read from nowhere.  That is a read, not a write, so it
    cannot corrupt anything -- but it can fault, and a fault costs a power
    cycle.  Flash or SRAM only.
    """
    if not (0x08000000 <= addr < 0x08100000 or 0x20000000 <= addr < 0x20020000):
        sys.exit("word_banks_ = 0x%08X is neither flash nor SRAM; refusing" % addr)
    if addr & 3:
        sys.exit("word_banks_ = 0x%08X is not word-aligned" % addr)
    return [(REGION_BANK, WORD_BANKS_OFF - BANK_WINDOW_BASE,
             struct.pack("<I", addr))]


def table_upload(banks):
    """`banks` = list of per-bank frame lists -> writes installing a RAM table.

    v5: the stream is placed AUTOMATICALLY.  STREAM_OFF never reaches the image
    -- a table entry carries an absolute pointer and the firmware decodes from
    wherever it points -- so the split between decode space and stream space is
    a per-upload decision, not a patch-time constant.

    One constraint ties them together: a bank's decode must not overrun the
    bitstream it is decoding from, and all streams coexist while only one bank
    decodes at a time.  So

        max_bank_frames * 14  +  sum(all bitstreams)  <=  MAGIC_OFF

    The stream therefore starts just above the largest bank's decode space,
    which leaves the most room for the streams themselves.  Order of writes is
    streams, table, then magic LAST -- the stub switches on the magic, so the
    table must be valid before it is enabled.
    """
    if not any(banks) or len(banks) > NUM_BANKS:
        sys.exit("give 1 to %d banks, got %d" % (NUM_BANKS, len(banks)))
    streams, report = [], []
    for i, frames in enumerate(banks):
        words = len(frames)
        if not words:
            streams.append(b"")
            continue
        if words > MAX_WORDS_PER_BANK:
            sys.exit("bank %d has %d words; the limit is %d"
                     % (i + 1, words, MAX_WORDS_PER_BANK))
        blob = S.encode_bank(frames)
        n = sum(len(f) for f in frames)
        streams.append(blob)
        report.append((i, words, n, len(blob)))
    max_frames = max(sum(len(f) for f in b) for b in banks if b)
    start = (max_frames * S.FRAME_BYTES + 3) & ~3          # word-aligned
    total = sum(len(b) for b in streams)
    if start + total > MAGIC_OFF:
        sys.exit("needs 0x%04X decode + %d stream = %d bytes, but only 0x%04X is "
                 "available below the table. Shorten the longest bank (%d frames) "
                 "or drop a bank." % (start, total, start + total, MAGIC_OFF, max_frames))
    streams += [b""] * (NUM_BANKS - len(streams))
    writes, off, entries = [], start, []
    for blob in streams:
        if not blob:
            entries.append(None)
            continue
        writes.append((REGION_FRAMES, off, blob))
        entries.append((FRAMES_BASE + off, len(blob)))
        off += len(blob)
    # A position without words points at the bank it repeats. An empty entry
    # does not play silence on hardware: it falls back to the vowel sounds.
    entries = [entries[s] for s in vocab.sources(streams)]
    table = b"".join(struct.pack("<II", p, n) for p, n in entries)
    writes.append((REGION_FRAMES, TABLE_OFF, table))
    writes.append((REGION_FRAMES, MAGIC_OFF, struct.pack("<I", MAGIC)))
    # And set word_banks_ (+0x00) ourselves, which v3's widened window allows.
    #
    # WITHOUT THIS the table is inert until the next Init, because Init is the
    # only thing that writes word_banks_ -- and Init already ran at the program
    # change, before the magic existed, so it installed the flash table. A Wave
    # change calls Load, not Init, so Load keeps reading flash. Measured
    # 2026-10-07: a Wave-only probe showed the upload having zero effect.
    #
    # So the two halves cover different moments: this write makes the table live
    # NOW, and the init stub re-installs it on every later Init so it survives.
    writes.append((REGION_BANK, WORD_BANKS_OFF - BANK_WINDOW_BASE,
                   struct.pack("<I", FRAMES_BASE + TABLE_OFF)))
    # LAST: invalidate loaded_bank_, or nothing ever re-decodes.
    #
    # The guard at 0x0805E646 tests `request == loaded_bank_` FIRST and skips
    # the load entirely when they match:
    #
    #     ldr r3,[r5,#0x8] ; cmp r11,r3 ; beq <skip>
    #
    # Setting word_banks_ above points Load at our table, but Load is never
    # called while the Wave knob stays put, because the request still equals
    # the bank already loaded. Measured 2026-10-08 in the emulator against the
    # owner's own panel 180 preset: after a full upload the WordBank still read
    # 21 words / 880 frames -- the untouched factory bank_4.
    #
    # Writing -1 here makes the next Render's guard see request(4) != -1, take
    # the `request < num_banks` branch, and decode OUR table. The bank window
    # already covers +0x08, so this costs one more message and no firmware
    # change.
    writes.append((REGION_BANK, LOADED_BANK_OFF - BANK_WINDOW_BASE,
                   struct.pack("<i", -1)))
    return writes, report, entries, start, total


def budget_report(report, entries, start, total):
    print("\n%-6s %6s %8s %8s %8s   %s" % ("bank", "words", "frames", "bytes", "seconds", "stream at"))
    print("-" * 62)
    used = {r[0]: r for r in report}
    for i, (ptr, size) in enumerate(entries):
        if i in used:
            _, words, n, nb = used[i]
            print("%-6d %6d %8d %8d %8.1f   0x%08X" % (i + 1, words, n, nb, n / S.FPS, ptr))
        else:
            src = [j for j, e in enumerate(entries) if e == (ptr, size)][0]
            print("%-6d %6s %8s %8s %8s   repeats bank %d" % (i + 1, "-", "-", "-", "-", src + 1))
    avail = MAGIC_OFF - start
    print("-" * 62)
    print("decode space  0x0000..0x%04X  (%d frames, the largest bank)"
          % (start, start // S.FRAME_BYTES))
    print("stream space  0x%04X..0x%04X  %d of %d bytes used (%.0f%%), %d free"
          % (start, MAGIC_OFF, total, avail, 100.0 * total / avail, avail - total))
    print("metadata      0x%04X..0x3800  magic + %d table entries"
          % (MAGIC_OFF, NUM_BANKS))
    print("word_banks_   set to 0x%08X now, and re-installed by the init stub "
          "on every Init" % (FRAMES_BASE + TABLE_OFF))


# Bank offsets the FIRMWARE owns: writing one is a REQUEST, and reading it
# back does not return what was written.
#
# `loaded_bank_` is the only one so far. A table payload writes -1 to force a
# re-decode, and `Load()` then stores the bank number it actually decoded -- so
# a correct, working upload reads back 4 where -1 was written. The first run of
# `verify` reported exactly that as a failure. Calling it a failure is wrong;
# skipping it silently would hide a real one, so it gets its own class and its
# own expectation: the value must be a bank number, or still -1 if nothing has
# rendered yet.
FIRMWARE_OWNED = {
    LOADED_BANK_OFF: ("loaded_bank_", "written as -1 to force a reload; Load() "
                      "replaces it with the bank it decoded"),
}


class _Ports:
    """rtmidi2's MidiOut and MidiIn calls, over a `microfreak.midi` transport.

    The low-level commands poll for replies themselves, so they want
    `get_message()`; `open_synth` gives a queue. This bridges the two, which
    lets them reach the emulator the same way every other command does.
    """

    def __init__(self, synth):
        self.synth = synth
        self.name = getattr(synth, "port_name", "the emulator")

    def send_message(self, message):
        self.synth.send(list(message))

    def get_message(self):
        try:
            return list(self.synth.recv(lambda m: True, timeout=0.001))
        except TimeoutError:
            return None


# The emulator runs slower than real time, so reply windows stretch with it.
SLOW = 10 if os.environ.get("MFREAK_EMULATOR") else 1


def open_midi(port="MicroFreak", need_input=True):
    """-> (out, in or None), with rtmidi2's send_message and get_message.

    Goes through `microfreak.midi.open_synth`, so MFREAK_EMULATOR applies.
    CoreMIDI needs a moment after opening before the input delivers; without
    it the first reply is lost, so the settle is not optional.
    """
    from microfreak.midi import open_synth
    ports = _Ports(open_synth(port))
    import time
    time.sleep(0.5)
    return ports, ports if need_input else None


def verify_upload(writes, port="MicroFreak", base=None, tries=3):
    """Read back every byte an upload claims to have written. -> exit code.

    The gap this closes: `send --probe` answers "is the instrument still
    alive", which is a much weaker question than "did the bytes land". v15
    passed every liveness probe and wrote garbage -- the magic read back as
    0x43FD54FD instead of 'MFVC' -- and that was only caught by peeking the
    region by hand afterwards. A payload file is already (region, offset,
    blob), so the check is generic: peek every word it covers and diff.

    Needs a peek-capable firmware, which means this verifies the upload, not
    the firmware. It checks VOICE 0 only; the handler also fans out to three
    other strides, and those are not where the engine reads from.

    Controls, both necessary:
      * Device Inquiry before and after -- a dead instrument would otherwise
        read as "every byte wrong".
      * a peek of the bank's `frames_` pointer, which must read FRAMES_BASE.
        Without it "all mismatched" cannot be told from "peek is not working",
        and that is exactly the confusion a verifier is for.
    """
    import time
    out, mi = open_midi(port)
    seq = [0]

    def alive():
        for _ in range(tries):
            while mi.get_message():
                pass
            out.send_message(list(INQUIRY_REQUEST))
            end = time.time() + 1.5 * SLOW
            while time.time() < end:
                m = mi.get_message()
                if m and bytes(m[:5]) == INQUIRY_REPLY_PREFIX:
                    return True
                if not m:
                    time.sleep(0.005)
        return False

    def peek(addr):
        for _ in range(tries):
            while mi.get_message():
                pass
            seq[0] = (seq[0] + 1) & 0x7F
            out.send_message(list(peek_message(addr, seq=seq[0], base=base)))
            end = time.time() + 2.0 * SLOW
            while time.time() < end:
                m = mi.get_message()
                if m:
                    v = peek_reply(bytes(m), seq=seq[0])
                    if v is not None:
                        return v
                elif not m:
                    time.sleep(0.005)
        return None

    if not alive():
        sys.exit("the instrument does not answer Device Inquiry. "
                 "Power-cycle it first.")
    print("control: Device Inquiry answers")
    anchor = peek(BANK_BASE + 0x94)
    if anchor != FRAMES_BASE:
        sys.exit("control FAILED: peek of the bank's frames_ pointer at "
                 "0x%08X read %s, expected 0x%08X. Either peek is not reaching "
                 "the handler (is opcode 0x%02X this firmware's?) or the bank "
                 "object is not where this build thinks. A diff would be "
                 "meaningless."
                 % (BANK_BASE + 0x94,
                    "no reply" if anchor is None else "0x%08X" % anchor,
                    FRAMES_BASE, PEEK_OPCODE))
    print("control: peek reads the frames_ pointer as 0x%08X, as it should\n"
          % anchor)

    total = good = 0
    failures, consumed = [], []
    for region, off, blob in writes:
        if region == REGION_BANK and off in FIRMWARE_OWNED and len(blob) == 4:
            name, why = FIRMWARE_OWNED[off]
            addr = BANK_BASE + off
            v = peek(addr)
            sv = None if v is None else (v - (1 << 32) if v >> 31 else v)
            ok = sv is not None and -1 <= sv < NUM_BANKS
            print("  %-18s 0x%08X  %4d bytes  firmware-owned: reads %s"
                  % ("bank+0x%04X" % off, addr, len(blob),
                     "no reply" if sv is None else "%d" % sv))
            print("      %s" % why)
            if not ok:
                print("      NOT a valid bank number -- that is a real failure")
                failures.append("%s (bank+0x%04X)" % (name, off))
            else:
                consumed.append("%s = %d" % (name, sv))
            continue
        dest = (BANK_BASE if region == REGION_BANK else FRAMES_BASE) + off
        lab = "%s+0x%04X" % ("bank" if region == REGION_BANK else "frames_", off)
        lo, hi = dest & ~3, (dest + len(blob) + 3) & ~3
        got = bytearray()
        missing = False
        for a in range(lo, hi, 4):
            v = peek(a)
            if v is None:
                missing = True
                got += b"\x00\x00\x00\x00"
            else:
                got += struct.pack("<I", v)
        window = bytes(got[dest - lo:dest - lo + len(blob)])
        bad = [i for i, (x, y) in enumerate(zip(window, blob)) if x != y]
        total += len(blob)
        good += len(blob) - len(bad)
        mark = "NO REPLY" if missing else ("%d of %d differ" % (len(bad), len(blob))
                                           if bad else "match")
        print("  %-18s 0x%08X  %4d bytes  %s" % (lab, dest, len(blob), mark))
        if bad:
            i = bad[0]
            print("      first at +%d: read %02X, expected %02X"
                  % (i, window[i], blob[i]))
            print("      read     %s" % window[:16].hex(" "))
            print("      expected %s" % bytes(blob[:16]).hex(" "))
            failures.append(lab)
        elif missing:
            failures.append(lab + " (unanswered peeks)")

    print()
    if not alive():
        print("control FAILED: the instrument stopped answering during the "
              "readback, so the result above is not trustworthy")
        return 2
    print("control: Device Inquiry still answers")
    print("%d of %d bytes read back identical across %d data write(s)"
          % (good, total, len(writes) - len(consumed)))
    for c in consumed:
        print("firmware-owned, consumed as intended: %s" % c)
    if failures:
        print("\nFAIL -- %s did not land" % ", ".join(failures))
        return 1
    print("\nPASS -- every byte the payload claims to have written is in RAM")
    return 0


def main():
    # The module docstring is the design document; this is the user's help.
    RAW = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(
        description="Upload speech vocabulary to patched firmware over SysEx.",
        epilog="    mfreak fw patch speech stock.mff --out speech.mff    once, then flash it\n"
               "    mfreak speech upload table --bank w1.wav w2.wav --out v.bin\n"
               "    mfreak speech upload dump v.bin                      show what it sends\n"
               "    mfreak speech upload send v.bin\n\n"
               "Most people want `mfreak speech say` instead. An upload lasts until\n"
               "the bank reloads: changing bank or switching engine away and back\n"
               "discards it.",
        formatter_class=RAW)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="show what an upload would send")
    d.add_argument("upload")
    vf = sub.add_parser("verify", help="read back what an upload wrote and compare",
                        description="Read back every byte an upload wrote and "
                                    "compare.")
    vf.add_argument("upload")
    vf.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    vf.add_argument("--peek-base", type=lambda x: int(x, 0), default=None,
                    metavar="ADDR",
                    help="the --peek-base the firmware was built with, if "
                         "not the default")
    w = sub.add_parser("wire", help="write an upload as raw SysEx")
    w.add_argument("upload"); w.add_argument("--out", required=True)
    tb = sub.add_parser("table", help="install a RAM bank table (needs the init stub)")
    tb.add_argument("--bank", action="append", nargs="+", default=[], metavar="WAV",
                    dest="banks", required=True,
                    help="one --bank per Wave position, each with its own words")
    tb.add_argument("--out", required=True)
    fz = sub.add_parser("freeze", help="stop the bank reloading, so uploads stick")
    fz.add_argument("--num-banks", type=int, default=0,
                    help="0 freezes (default); 5 restores factory reloading")
    fz.add_argument("--out", required=True)
    s = sub.add_parser("send", help="send an upload to the instrument",
                       description="Send an upload to the instrument.")
    s.add_argument("upload")
    s.add_argument("--port", default="MicroFreak",
                   help="MIDI port name substring (default: MicroFreak)")
    s.add_argument("--magic", type=lambda x: int(x, 0), default=None, metavar="BYTE",
                   help="override the magic byte (default: 0x%02X). A wrong "
                        "one sends every message but writes nothing, for "
                        "debugging freezes" % OPCODE_MAGIC)
    s.add_argument("--probe", action="store_true",
                   help="check the instrument still answers after each "
                        "message, to find where a freeze happens")
    s.add_argument("--spacing", type=float, default=250.0, metavar="MS",
                   help="ms between messages (default: 250; faster drops "
                        "messages)")
    a = ap.parse_args()

    if a.cmd == "verify":
        return verify_upload(unpack_upload(open(a.upload, "rb").read()),
                             port=a.port, base=a.peek_base)

    elif a.cmd == "wire":
        writes = unpack_upload(open(a.upload, "rb").read())
        msgs = messages(writes)
        open(a.out, "wb").write(b"".join(msgs))
        print("%d messages, %d wire bytes -> %s"
              % (len(msgs), sum(len(m) for m in msgs), a.out))

    elif a.cmd == "table":
        import mfreak_speech.analyze as A
        banks = []
        for i, group in enumerate(a.banks):
            frames = []
            for w in group:
                fr = A.analyse(A.load_audio(w), quantise=True)   # the bitstream ships
                frames.append(fr)
                print("  bank %d  %-26s %3d frames" % (i + 1, os.path.basename(w), len(fr)))
            banks.append(frames)
        writes, report, entries, start, total = table_upload(banks)
        budget_report(report, entries, start, total)
        open(a.out, "wb").write(pack_upload(writes))
        print("\n%d SysEx messages -> %s" % (len(messages(writes)), a.out))
        print("the Wave knob now selects between these banks, and a reload "
              "re-decodes them instead of destroying them")

    elif a.cmd == "freeze":
        writes = freeze_writes(a.num_banks)
        open(a.out, "wb").write(pack_upload(writes))
        print("num_banks -> %d  (bank+0x%02X)" % (a.num_banks, NUM_BANKS_OFF))
        print("%d message -> %s" % (len(messages(writes)), a.out))
        if a.num_banks == 0:
            print("the firmware will not reload a bank again until Init runs "
                  "(power cycle); the Wave knob stops switching banks")

    elif a.cmd == "dump":
        writes = unpack_upload(open(a.upload, "rb").read())
        msgs = messages(writes)
        print("%d writes -> %d messages" % (len(writes), len(msgs)))
        for region, off, blob in writes:
            if region == REGION_BANK:
                vals = struct.unpack_from("<%dI" % (len(blob) // 4), blob, 0)
                at = BANK_WINDOW_BASE + off
                if at == WORD_BANKS_OFF and len(vals) == 1:
                    print("  bank+0x%02X      %4d bytes  word_banks_ = 0x%08X"
                          % (at, len(blob), vals[0]))
                elif at == NUM_BANKS_OFF and len(vals) == 1:
                    print("  bank+0x%02X      %4d bytes  num_banks = %d%s"
                          % (at, len(blob), vals[0],
                             "   (0 = never reload)" if vals[0] == 0 else ""))
                elif at == LOADED_BANK_OFF and len(vals) == 1:
                    print("  bank+0x%02X      %4d bytes  loaded_bank_ = %d%s"
                          % (at, len(blob), vals[0] - (1 << 32)
                             if vals[0] >= (1 << 31) else vals[0],
                             "   (-1 forces the re-decode)"
                             if vals[0] == 0xFFFFFFFF else ""))
                elif len(vals) < 2:
                    print("  bank+0x%02X      %4d bytes  = %s"
                          % (at, len(blob), [hex(v) for v in vals]))
                else:
                    print("  bank+0x%02X      %4d bytes  frames=%d words=%d bounds=%s%s"
                          % (at, len(blob), vals[0], vals[1],
                             list(vals[2:8]), " ..." if len(vals) > 8 else ""))
            else:
                print("  frames_+0x%04X  %4d bytes  (frame %d, %d frames)"
                      % (off, len(blob), off // S.FRAME_BYTES,
                         len(blob) // S.FRAME_BYTES))
        print("first message (%d bytes): %s" % (len(msgs[0]), msgs[0][:24].hex(" ") + " ..."))

    # `send` by name, not by falling off the end of the chain. This was
    # `else:`, so a subcommand added without a branch did not error -- it
    # reached the MIDI SENDER and transmitted whatever `--upload` held. The
    # dispatch is explicit now and an unknown command is a bug, not a send.
    elif a.cmd == "send":
        import time
        writes = unpack_upload(open(a.upload, "rb").read())
        out, probe = open_midi(a.port)
        if not a.probe:
            probe = None
        msgs = messages(writes)
        if a.magic is not None and a.magic != OPCODE_MAGIC:
            # Rewrite only wire byte 7, so the messages are otherwise identical
            # to the ones that work.
            msgs = [bytes(m[:7]) + bytes([a.magic & 0x7F]) + bytes(m[8:])
                    for m in msgs]
            print("MAGIC OVERRIDDEN to 0x%02X (the handler wants 0x%02X).\n"
                  "Every message should reach the handler and bail, writing "
                  "NOTHING." % (a.magic, OPCODE_MAGIC))
        # Spacing is not optional: the firmware drops messages when pushed
        # faster, and the loss is a function of rate, not of total volume
        # (71af894).  250 ms is the value verified byte-for-byte.
        if a.spacing < WIRE_MS_PER_MESSAGE:
            sys.exit("--spacing %g ms is below the %.1f ms each message takes "
                     "on the wire" % (a.spacing, WIRE_MS_PER_MESSAGE))
        # Print each message BEFORE sending it, flushed. If the instrument
        # freezes mid-upload the last number on screen is the message that did
        # it, which is the one fact a freeze otherwise destroys -- the old
        # summary printed only after all of them were away, so a freeze left no
        # indication of where. Each line names the destination so the number
        # can be read against `dump`.
        import sys as _sys
        if probe is not None:
            def alive(timeout=1.0 * SLOW):
                """-> True if the instrument answers a device inquiry."""
                while probe.get_message():
                    pass                        # drain anything stale
                out.send_message(list(INQUIRY_REQUEST))
                end = time.time() + timeout
                while time.time() < end:
                    msg = probe.get_message()
                    if msg and bytes(msg[:5]) == INQUIRY_REPLY_PREFIX:
                        return True
                    if not msg:
                        time.sleep(0.005)
                return False

            # POSITIVE CONTROL. If a healthy instrument does not answer, every
            # "dead" below would be meaningless -- the probe would be measuring
            # itself. Refuse to run rather than produce that.
            if not alive():
                sys.exit("the instrument does not answer a device inquiry BEFORE "
                         "any upload, so the probe cannot tell live from dead.\n"
                         "Power cycle and retry; if it still does not answer, the "
                         "probe is wrong and nothing it reports can be trusted.")
            print("probe: instrument answers before upload (positive control OK)")

        for i, m in enumerate(msgs):
            if i:
                time.sleep(a.spacing / 1000.0)
            dest = (m[9] << 7) | m[10]
            region = "bank" if (m[11] & REGION_BANK) else "frames_"
            print("  -> %2d/%d  %s+0x%04X  %d bytes"
                  % (i + 1, len(msgs), region, dest, m[11] & 0x3F), end="")
            _sys.stdout.flush()
            out.send_message(list(m))
            if probe is not None:
                ok = alive()
                print("   %s" % ("alive" if ok else "*** NO REPLY ***"))
                _sys.stdout.flush()
                if not ok:
                    print()
                    print("The instrument stopped answering after message %d/%d "
                          "(%s+0x%04X)." % (i + 1, len(msgs), region, dest))
                    print("Power cycle to recover; nothing sent here is persistent.")
                    sys.exit(1)
            else:
                print()
        print("sent %d messages to %s at %g ms spacing (%.1f s)"
              % (len(msgs), out.name, a.spacing,
                 (len(msgs) - 1) * a.spacing / 1000.0))
        print("Play a note on Speech before uploading, so the bank is loaded. "
              "Do not change bank afterwards.")

    else:
        raise AssertionError(
            "no branch for `speech upload %s`. argparse accepted it, so the "
            "subparser exists and the dispatch above is missing a case."
            % a.cmd)


if __name__ == "__main__":
    main()
