#!/usr/bin/env python3
"""Read MCC's exports: `.mfprojz` (presets), `.mfsbz` (samples),
`.mfwbz` (wavetables) -- and the single-item `.mfpz` / `.mfsz` / `.mfwz`.

All of them are ZIP archives of **Boost.Serialization text archives**, one
per item, under `<Bank>/<NN-Bank-A>/<slot>-<project>-A<n>.<ext>`.  The inner
format is the one docs/SAMPLE_ENCODING.md already documents; a bank is just many
of them, so nothing here decodes new content -- it only walks the container.

The archive header, confirmed across all 512 presets, 128 samples and 16
wavetables of one factory set:

    <22> serialization::archive <10> <0> <4>
    <L> <version>          "94", "16", "108", "48", "DEVBUILD"
    <L> <name>             MAY CONTAIN SPACES -- read by character count,
                           never by splitting on whitespace
    <n> <0> <0> <18> <18-char bit string>      category/tag flags
    <a> <b> <c> <N> <N signed ints>

Item payloads, by extension:

    .mbp / .mfpz    4672 signed ints = the raw wire preset payload, exactly
                    what `preset.read` returns (preset.fields.rows_from_payload)
    .mfw / .mfwz    16384 = 8192 x int16 LE, one wavetable
    .mfs / .mfsz    N     = 16-bit LE PCM at 32 kHz, mono

A `.mbp` carries **no region 0** -- no name record, no schema byte -- so
placing one in flash needs a region 0 from somewhere else.  See the authoring
trap in microfreak-emu/docs/FORMATS.md before borrowing one.

Slot numbers live only in the filename, and MCC drops the separating dash
when it feels like it (47 of 512 in one bank: `10600122021 17h55-A26.mbp` is
slot 106).  slots() resolves that against the constraint that a bank's slots
are a permutation of 1..N.

usage:
    mfreak asset info BANK.mfprojz            # list preset, name, engine
    mfreak asset info BANK.mfsbz --hashes     # list samples with SmpHash
    mfreak asset info BANK.mfwbz --extract DIR  # dump each item's ints as PCM
"""
import argparse
import os
import re
import sys
import zipfile
from microfreak import jsonout

ZEROS_LEN = 18
PRESET_WIRE_LEN = 4672
WAVETABLE_INTS = 16384

# MCC's "Characteristics" grid, in the order its own UI lists them. The tag
# string stores these REVERSED -- string position 0 is Soundtrack and position
# 17 is Acid. See characteristics() for how that was established.
CHARACTERISTICS = ("Acid", "Aggressive", "Ambient", "Bizarre", "Bright", "Complex",
                   "Dark", "Digital", "Ensemble", "Funky", "Hard", "Long",
                   "Noise", "Quiet", "Short", "Simple", "Soft", "Soundtrack")

# MCC's "Type" dropdown, in its own order, which is alphabetical. `scalars[0]`
# is a 0-based index into this -- and unlike CHARACTERISTICS it is NOT
# reversed. See preset_type().
TYPES = ("Bass", "Brass", "Keys", "Lead", "Organ", "Pad", "Percussion",
         "Sequence", "SFX", "Strings", "Template", "Vocoder")

ARCHIVE_MAGIC = "serialization::archive"
ARCHIVE_HEAD = ("10", "0", "4")        # archive and class versions; 656/656 items
NO_TAGS = "0" * ZEROS_LEN
EMPTY_SCALARS = ("1", "0", "0")        # what MCC writes for an unused slot
EMPTY_ABC = ("1", "0", "1")


class Archive:
    """Every field of one Boost text archive, enough to rebuild it exactly.

    `parse_archive()` throws away the scalars it does not need, which is fine
    for reading and useless for writing. This keeps them, so a bank can be
    read, edited and written back byte-identically.

        <22> serialization::archive <10> <0> <4>
        <L> <version>  <L> <name>
        <s0> <s1> <s2>  <18> <tags>
        <a> <b> <c>  <N>  <N signed ints>

    Measured across all 512 presets, 128 samples and 16 wavetables of one
    factory set: `head` is always ("10", "0", "4"), the tag string is always
    18 characters, the array count is always exactly `<N>` followed by N ints
    (no container version marker ever appears), and the text always ends in a
    single newline. `s0` is the save-screen category; `c` is the schema byte.
    """

    __slots__ = ("head", "version", "name", "scalars", "tags", "abc", "values")

    def __init__(self, version, name, tags, values,
                 scalars=("0", "0", "0"), abc=("0", "0", "0"), head=ARCHIVE_HEAD):
        self.head = tuple(head)
        self.version = version
        self.name = name
        self.scalars = tuple(scalars)
        self.tags = tags
        self.abc = tuple(abc)
        self.values = values

    @property
    def schema(self):
        """The schema byte: `abc[2]`. High nibble 3 if the payload holds a Voc
        module and 1 if not -- confirmed on all 384 populated factory presets,
        the same rule `preset.write` applies to region 0."""
        return int(self.abc[2])


def preset_type(scalars):
    """-> the MCC "Type" name for an archive's scalars, or None if out of range.

    `scalars[0]` is a 0-based index into MCC's Type dropdown, which is
    alphabetical. Established 2026-09-17 from three presets the owner read off
    MCC -- `NervousKeys` = Keys (2), `Tubular` = Percussion (6),
    `Disrespectful` = SFX (8) -- all three of which the table reproduces, plus
    `K01 Speech` importing as Bass with the 0 we wrote.

    Corroborated independently of those: of the 75 factory presets whose name
    contains exactly one type word, 38 carry the matching type (51%), against
    6.2 of 75 expected under a random ordering of the twelve names (4000
    permutations, P(random >= observed) < 0.0001). The misses are curation
    calls rather than decoding errors -- `ArpZap` is filed as Lead, not
    Sequence.

    The factory bank uses all twelve values and nothing else.

    Note the asymmetry with the tag string: Characteristics are stored
    reversed, Types are not.
    """
    v = int(scalars[0])
    return TYPES[v] if 0 <= v < len(TYPES) else None


def type_value(name):
    """-> the `scalars[0]` value for an MCC Type name."""
    if name not in TYPES:
        raise ValueError("unknown type %r (known: %s)" % (name, ", ".join(TYPES)))
    return TYPES.index(name)


def characteristics(tags):
    """-> the set of MCC "Characteristics" a tag string asserts.

    The 18 bits are the 18 boxes of MCC's Characteristics grid, stored in
    **reverse** order: string position 0 is Soundtrack, position 17 is Acid.

    Established 2026-09-17. `Roid` stores `000101001000000000`; MCC shows
    Funky, Noise and Short for it, which the reversed reading reproduces
    exactly and the left-to-right reading does not (it gives Bizarre, Complex,
    Ensemble). Corroborated across all 38 tagged factory presets by a test
    that does not depend on that one screenshot: count presets asserting a
    contradictory pair (Soft+Hard, Short+Long, Simple+Complex, Bright+Dark,
    Quiet/Ambient+Aggressive).

        reversed reading      0 of 38 contradictory
        left-to-right         6 of 38
        random bit orders     3.7 on average (4000 permutations)

    P(a random bit order is at least as consistent as reversed) = 0.038. The
    reversed reading also pairs names with meanings the other does not:
    `Digi Express` reads Digital+Aggressive rather than Hard+Soft, `Smooth
    IPA` reads Soft+Long+Ambient, `Raver` reads Aggressive+Dark+Digital+
    Hard+Noise.
    """
    if len(tags) != ZEROS_LEN:
        raise ValueError("tag string must be %d characters" % ZEROS_LEN)
    return {CHARACTERISTICS[ZEROS_LEN - 1 - i]
            for i, c in enumerate(tags) if c == "1"}


def tag_string(names=()):
    """-> the 18-character tag string asserting these Characteristics."""
    bits = ["0"] * ZEROS_LEN
    for n in names:
        if n not in CHARACTERISTICS:
            raise ValueError("unknown characteristic %r (known: %s)"
                             % (n, ", ".join(CHARACTERISTICS)))
        bits[ZEROS_LEN - 1 - CHARACTERISTICS.index(n)] = "1"
    return "".join(bits)


def build_archive(ar):
    """-> the exact text of one Boost archive. The inverse of parse_full()."""
    if len(ar.tags) != ZEROS_LEN:
        raise ValueError("tags must be %d characters, got %d"
                         % (ZEROS_LEN, len(ar.tags)))
    out = ["%d %s" % (len(ARCHIVE_MAGIC), ARCHIVE_MAGIC), " ".join(ar.head),
           "%d %s" % (len(ar.version), ar.version),
           "%d %s" % (len(ar.name), ar.name),
           " ".join(ar.scalars),
           "%d %s" % (ZEROS_LEN, ar.tags),
           " ".join(ar.abc),
           str(len(ar.values))]
    text = " ".join(out)
    if ar.values:
        text += " " + " ".join(str(v) for v in ar.values)
    return text + "\n"


def schema_byte(wire, low_nibble=0x3):
    """The schema byte for a preset payload, derived from the payload itself.

    High nibble 3 if the payload carries a `Voc` module, else 1 -- the rule
    `preset.write` uses for region 0, and confirmed here against all 384
    populated presets of the factory bank.

    The low nibble is a format vintage. In the factory bank it tracks the
    version string (ver "48" is always 3, "108" almost always 2, "16" is 0 or
    1); on the device every one of 509 saved slots reads 3, so 3 is the
    default -- it is what the instrument itself writes.
    """
    import microfreak.preset.tlv as preset_tlv
    decoded = bytes(b & 0xFF for b in preset_tlv.decode_7to8(list(wire)))
    has_voc = any(m == "Voc" for m, _, _ in preset_tlv.fields(decoded))
    return ((3 if has_voc else 1) << 4) | (low_nibble & 0xF)


class Item:
    """One item out of a bank: where it came from, and what it holds."""

    def __init__(self, path, slot, version, name, tags, values, archive=None):
        self.path = path            # name inside the archive
        self.slot = slot            # 1-based, as the panel shows it
        self.version = version
        self.name = name
        self.tags = tags            # 18-character bit string
        self.values = values        # signed ints, as the archive stores them
        self.archive = archive      # every field, for writing the item back

    def __repr__(self):
        return "Item(slot=%r, name=%r, n=%d)" % (self.slot, self.name, len(self.values))

    def is_empty(self):
        """MCC exports an unused slot as a one-element array."""
        return len(self.values) <= 1

    def wire(self):
        """The preset payload as unsigned wire bytes (`preset.read`'s shape)."""
        if len(self.values) != PRESET_WIRE_LEN:
            raise ValueError("%s holds %d values, not a %d-byte preset payload"
                             % (self.path, len(self.values), PRESET_WIRE_LEN))
        return bytes(v & 0xFF for v in self.values)

    def pcm(self):
        """The audio as raw 16-bit LE PCM bytes (samples and wavetables)."""
        return bytes(v & 0xFF for v in self.values)

    def sample_hash(self):
        """Gen/SmpHash for this sample: the device's own content hash.

        Measured 2026-09-11 -- a plain 16-bit sum of the internal signed
        bytes. Exact on all 30 (hash, index) pairs in the capture.
        """
        return sum(self.values) & 0xFFFF


def parse_archive(text):
    """-> (version, name, tags, values) for one Boost text archive."""
    ar = parse_full(text)
    return ar.version, ar.name, ar.tags, ar.values


def parse_full(text):
    """-> Archive, keeping every field so the text can be rebuilt exactly.

    The array is `<count>` then exactly `count` ints. An earlier version of
    this parser searched for "the count that fits", because a container
    version marker was thought to sometimes precede it. Measured across all
    656 items of one factory set, no marker ever appears and the structure is
    unconditional -- and the search was actively wrong on empty slots, whose
    tail `1 0 1 0` it read as one value of 0 rather than the zero values the
    count states.
    """
    i, n = 0, len(text)

    def token():
        nonlocal i
        while i < n and text[i].isspace():
            i += 1
        j = i
        while j < n and not text[j].isspace():
            j += 1
        t = text[i:j]
        i = j
        return t

    def string(length):
        nonlocal i
        while i < n and text[i].isspace():
            i += 1
        s = text[i:i + length]
        i += length
        return s

    if string(int(token())) != ARCHIVE_MAGIC:
        raise ValueError("not a Boost text archive")
    head = (token(), token(), token())             # archive/class versions
    version = string(int(token()))
    name = string(int(token()))
    scalars = (token(), token(), token())          # s0 = save-screen category
    tags = string(int(token()))                    # the 18-bit flag string
    abc = (token(), token(), token())              # abc[2] = the schema byte
    count = int(token())
    values = []
    for _ in range(count):
        t = token()
        if not t:
            raise ValueError("array claims %d values, ran out at %d"
                             % (count, len(values)))
        values.append(int(t))
    if token():
        raise ValueError("trailing tokens after a %d-value array" % count)
    return Archive(version, name, tags, values, scalars, abc, head)


# A bank's slots are the instrument's slots: 512 presets, and fewer for the
# sample and wavetable banks. 512 is used as the ceiling for all three because
# it only has to be an upper bound -- `_slots` is pinning a slot number, not
# validating one against the right bank kind.
#
# SLOT_COUNT, not MAX_SLOT, and the rename is the point. `MAX_SLOT` is 511 in
# preset/select.py and in microfreak-emu's mfrender.py, where it means the
# highest 0-BASED slot; here the same name meant 512, the highest 1-based slot
# NUMBER as it appears in an archive member's filename. Two meanings of one
# identifier, differing by one, across the 0-based/1-based boundary that is
# this project's most-repeated bug -- `asset info --json` shipped a
# double-increment on exactly that seam. Both values were right in place; the
# collision was the hazard.
SLOT_COUNT = 512


def _slots(names):
    """-> {archive name: slot}, resolving MCC's missing separator dash.

    Every name starts with its slot number. MCC usually writes a "-" after
    it, and sometimes does not: in `factory_6_0_preset_bank.mfprojz` 47 of
    512 members read `10600122021 17h55-A26.mbp`, where the project name
    itself begins with a digit and the separator is gone. Those are pinned by
    elimination, since a full bank's slots are a permutation of 1..N.

    **The dashed case is bounded by the instrument, not by the member count.**
    It used to be bounded by `len(names)`, which made a bank of fewer than ten
    items unreadable by the same code that wrote it: `write_bank` zero-pads to
    at least two digits, so a five-item bank's `01-...` has a two-digit prefix
    while `len(str(5))` is one, and the name was sent down the ambiguous path
    where `int("0")` is out of range and nothing is left to pin. A sparse bank
    failed the same way -- five items at slots 101-105 offered only the
    candidate `1`. Slots come from the device, so `SLOT_COUNT` is what bounds
    them; the member count is not a bound on anything.
    """
    out, ambiguous = {}, {}
    limit = len(names)
    for path in names:
        base = os.path.basename(path)
        digits = re.match(r"(\d+)", base)
        if not digits:
            raise ValueError("%s does not start with a slot number" % base)
        d = digits.group(1)
        if base[len(d):len(d) + 1] == "-" and 1 <= int(d) <= SLOT_COUNT:
            out[path] = int(d)
        else:
            # No dash to trust. Only MCC writes these, and only in full
            # banks, so bounding the candidates by the member count is still
            # right here -- it is what makes elimination work at all.
            #
            # Reviewed 2026-10-18, because the asymmetry with SLOT_COUNT above
            # reads like half a fix. Measured: a sparse bank with a dashless
            # name for slot 105 cannot be pinned under EITHER bound --
            # len(names) leaves no candidate, SLOT_COUNT leaves two. Widening it
            # changes the complaint, not the outcome, because elimination
            # needs the slots to be a permutation of 1..N and a sparse bank is
            # not one. write_bank always writes the dash, so nothing this
            # project produces can reach the case at all.
            ambiguous[path] = [int(d[:k]) for k in range(1, len(str(limit)) + 1)
                               if k <= len(d) and 1 <= int(d[:k]) <= limit]
    taken = set(out.values())
    if len(taken) != len(out):
        raise ValueError("duplicate slot numbers in the bank")
    for path, candidates in ambiguous.items():
        free = [c for c in candidates if c not in taken]
        if len(free) != 1:
            raise ValueError(
                "%s: cannot tell which slot this is (candidates %s, free %s; "
                "the bank has %d members). Rename it to put a dash after its "
                "slot number, or re-export the bank."
                % (os.path.basename(path), candidates, free, len(names)))
        out[path] = free[0]
        taken.add(free[0])
    return out


def item_members(z):
    """-> the item files in an export's zip, without folders or the hidden
    files macOS adds (.DS_Store, __MACOSX/). MCC zips the folder as it is on
    disk, so its own exports can contain them."""
    return [m for m in z.namelist()
            if not m.endswith("/") and not m.startswith("__MACOSX/")
            and not m.rsplit("/", 1)[-1].startswith(".")]


def first_bank(members, path):
    """-> the members of the first bank folder.

    A project can hold several banks (`01-<name>-A/`, `02-<name>-B/`, ...).
    The MicroFreak has one, and MCC reads only the first and ignores the
    rest, so this does the same and says so.
    """
    folders = sorted({m.rsplit("/", 1)[0] if "/" in m else "" for m in members})
    if len(folders) > 1:
        print("warning: %s holds %d banks. Only the first, %s, is read, as "
              "MIDI Control Center does." % (path, len(folders), folders[0]),
              file=sys.stderr)
    return [m for m in members
            if (m.rsplit("/", 1)[0] if "/" in m else "") == folders[0]]


def read_bank(path):
    """-> [Item] for a `.mfprojz` / `.mfbz` / `.mfsbz` / `.mfwbz`, by slot."""
    with zipfile.ZipFile(path) as z:
        members = item_members(z)
        if not members:
            raise ValueError("%s: empty archive" % path)
        members = first_bank(members, path)
        slots = _slots(members)
        items = []
        for m in members:
            ar = parse_full(z.read(m).decode("latin-1"))
            items.append(Item(m, slots[m], ar.version, ar.name, ar.tags,
                              ar.values, ar))
    return sorted(items, key=lambda it: it.slot)


def empty_preset(name):
    """An unused preset slot, as MCC writes one: no payload."""
    return Archive(version="48", name=name, tags=tag_string(), values=[],
                   scalars=("1", "0", "0"), abc=("1", "0", "1"))


def write_bank(path, items, top="MFGen", subdir="01-mfgen-A", ext=None,
               pad=True):
    """Write [(slot, Archive)] as a `.mfprojz` / `.mfsbz` / `.mfwbz`.

    Member paths follow MCC's own convention,

        <top>/<subdir>/<slot>-<project>-A<n>.<ext>

    with the slot zero-padded to the bank's width and the project taken from
    the archive's own name. Slots here are **1-based**, as the panel shows
    them and as `_slots()` reads them back: device slot 0 is bank slot 1.

    The container is a plain deflated ZIP. MCC's own timestamps and zlib
    settings are not reproduced and do not need to be -- what has to be exact
    is each member's archive text, which `--round-trip` checks.
    """
    if ext is None:
        ext = {".mfprojz": "mbp", ".mfsbz": "mfs",
               ".mfwbz": "mfw"}.get(os.path.splitext(path)[1])
        if ext is None:
            raise ValueError("cannot infer an item extension for %s" % path)
    items = sorted(items, key=lambda kv: kv[0])
    slots = [s for s, _ in items]
    if len(set(slots)) != len(slots):
        raise ValueError("duplicate slot numbers")
    if slots and min(slots) < 1:
        raise ValueError("bank slots are 1-based; got %d" % min(slots))
    # MCC places a preset by its position among the members, sorted by
    # number, not by the number itself (microfreak-emu/docs/FORMATS.md, "How
    # MCC assigns slots"). So every slot before the last needs a member: a
    # gap is filled with an empty preset, which MCC also empties on the
    # instrument. Zero-padding to a fixed width keeps string and numeric
    # order the same. _slots() reads the number itself.
    if pad and ext == "mbp" and slots:
        bank_name = re.sub(r"^\d+-|-[A-Z]$", "", subdir) or top
        have = set(slots)
        items = sorted(items + [(s, empty_preset("%s-A%d" % (bank_name, s)))
                                for s in range(1, max(slots) + 1)
                                if s not in have], key=lambda kv: kv[0])
    width = max(2, len(str(max(slots)))) if slots else 2
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("%s/" % top, b"")
        z.writestr("%s/%s/" % (top, subdir), b"")
        for slot, ar in items:
            # MCC also drops the separating dash on 47 of 512 factory names,
            # which _slots() has to guess around; always writing it avoids that.
            safe = re.sub(r"[/\\]", "_", ar.name).strip() or "preset"
            member = "%s/%s/%0*d-%s-A%0*d.%s" % (top, subdir, width, slot,
                                                 safe, width, slot, ext)
            z.writestr(member, build_archive(ar).encode("latin-1"))
    return path


def round_trip(path):
    """Re-serialise every item of a bank and compare against the original.

    The decisive test for the writer: if `build_archive(parse_full(t)) == t`
    for every member, the serialiser reproduces MCC's own bytes and the only
    remaining unknown is whether MCC accepts a bank it did not write.

    -> (checked, [(member, reason)]) -- an empty list of failures is a pass.
    """
    bad = []
    with zipfile.ZipFile(path) as z:
        members = item_members(z)
        for m in members:
            original = z.read(m).decode("latin-1")
            try:
                rebuilt = build_archive(parse_full(original))
            except Exception as exc:                        # noqa: BLE001
                bad.append((m, "parse/build raised %s" % exc))
                continue
            if rebuilt != original:
                where = next((k for k in range(min(len(rebuilt), len(original)))
                              if rebuilt[k] != original[k]), min(len(rebuilt), len(original)))
                bad.append((m, "differs at byte %d: %r vs %r"
                            % (where, original[where:where + 24],
                               rebuilt[where:where + 24])))
    return len(members), bad


def read_item(path):
    """-> Item for a single-item export (`.mfpz`, `.mfsz`, `.mfwz`) or a bare
    archive file (`.mbp`, `.mfs`, `.mfw`) already extracted from a bank."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            member = item_members(z)[0]
            text = z.read(member).decode("latin-1")
    else:
        member = path
        text = open(path, encoding="latin-1").read()
    ar = parse_full(text)
    return Item(member, None, ar.version, ar.name, ar.tags, ar.values, ar)


def main():
    # The module docstring documents the format; this is the user's help.
    ap = argparse.ArgumentParser(
        description="Show what is in an MCC preset, sample or wavetable export.",
        epilog="    mfreak asset info BANK.mfprojz               presets, names, engines\n"
               "    mfreak asset info BANK.mfsbz --hashes        samples with hashes\n"
               "    mfreak asset info BANK.mfwbz --extract DIR   save each item\n\n"
               "--extract saves each item as its own MCC file (.mfpz, .mfsz "
               "or .mfwz), named by slot.\n"
               "Also reads single-item .mfpz, .mfsz and .mfwz files. Offline.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", metavar="FILE",
                    help="an MCC export: .mfprojz (presets), .mfsbz "
                         "(samples) or .mfwbz (wavetables)")
    ap.add_argument("--hashes", action="store_true",
                    help="print each sample's hash")
    ap.add_argument("--extract", metavar="DIR",
                    help="save each item to DIR as its own MCC file")
    ap.add_argument("--empty", action="store_true", help="list empty slots too")
    ap.add_argument("--characteristics", action="store_true",
                    help="show each preset's MCC Characteristics")
    ap.add_argument("--round-trip", action="store_true",
                    help="check that every item re-serialises byte for byte")
    args = ap.parse_args()

    if args.round_trip:
        checked, bad = round_trip(args.export)
        for m, why in bad[:10]:
            print("FAIL  %s\n      %s" % (m, why))
        print("%s: %d/%d items re-serialise byte-identically"
              % (os.path.basename(args.export), checked - len(bad), checked))
        return 0 if not bad else 1

    single = os.path.splitext(args.export)[1].lower() in (".mfpz", ".mfsz", ".mfwz")
    if single and args.extract:
        ap.error("%s holds one item; --extract is for banks" % args.export)
    items = [read_item(args.export)] if single else read_bank(args.export)
    engine = None
    # The any() already covers items[0], and an empty list makes it False,
    # so the leading `items and len(items[0]...)` was never load-bearing.
    if any(len(it.values) == PRESET_WIRE_LEN for it in items):
        import microfreak.preset.fields as PF
        engine = PF

    if args.extract:
        os.makedirs(args.extract, exist_ok=True)
    shown = 0
    rows = []
    for it in items:
        if it.is_empty() and not args.empty:
            continue
        shown += 1
        extra = ""
        if engine and len(it.values) == PRESET_WIRE_LEN:
            extra = "  %s" % engine.osc_engine(engine.rows_from_payload(it.wire()))
        if args.hashes and len(it.values) > 1:
            extra += "  hash=0x%04X" % it.sample_hash()
        if args.characteristics:
            extra += "  %s" % (preset_type(it.archive.scalars) or "?")
            named = characteristics(it.tags)
            extra += "  [%s]" % ", ".join(sorted(named)) if named else "  [-]"
        print("%4s  %-24r %-9s %7d%s" % ("-" if it.slot is None else it.slot,
                                         it.name, it.version,
                                         len(it.values), extra))
        # Item.slot is 1-BASED -- it is the number in the archive member's
        # own filename, and bank slot 1 is device slot 0 (see read_bank).
        # The JSON contract is the other way round: "slot" is the 0-based
        # number `preset write` takes, "panel" is what the instrument shows.
        # This used to emit slot=it.slot and panel=it.slot+1, so a caller
        # feeding "slot" to `preset write` overwrote the NEXT preset up.
        rows.append({"slot": None if it.slot is None else it.slot - 1,
                     "panel": it.slot, "name": it.name,
                     "version": it.version, "values": len(it.values),
                     "detail": extra.strip() or None})
        if args.extract and not it.is_empty():
            from microfreak import mccfile
            kind = ("preset" if len(it.values) == PRESET_WIRE_LEN
                    else mccfile.BANKS.get(os.path.splitext(args.export)[1].lower(),
                                           "sample"))
            stem = "%03d-%s" % (it.slot, re.sub(r"[^A-Za-z0-9._-]+", "_", it.name))
            mccfile.write_one(os.path.join(args.extract,
                                           stem + mccfile.SINGLE_EXT[kind]),
                              it.archive, kind)
    print("\n%d of %d slots in use" % (shown, len(items)), file=sys.stderr)
    jsonout.result(file=args.export, in_use=shown, slots=len(items),
                   extracted_to=args.extract, items=rows)


if __name__ == "__main__":
    sys.exit(main() or 0)
