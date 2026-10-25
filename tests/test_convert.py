#!/usr/bin/env python3
"""Presets from older firmware convert to newer formats correctly.

    uv run python tests/test_convert.py

Offline, under a second. tests/fixtures/init-*.mfpz are the Init patch each
firmware format saves, from microfreak-emu's virtual front panel; the 5.0.0
one is byte-identical to the Init a real instrument saved.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import microfreak.bank as B                                    # noqa: E402
import microfreak.preset.convert as C                          # noqa: E402
import microfreak.preset.tlv as T                              # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
VERSIONS = {0x10: "1.0.5", 0x11: "2.0.3", 0x30: "2.1.3", 0x31: "3.0.6",
            0x32: "4.0.3", 0x33: "5.0.0"}
# (engine count, stored value) -> the engine a 5.0 instrument showed after
# conversion, read off its screen for seven factory presets.
ON_THE_INSTRUMENT = [(12, 16384, "V. Analog"), (13, 32767, "Noise"),
                     (14, 4681, "Superwave"), (17, 30840, "SawX"),
                     (18, 12743, "Waveshaper"), (12, 27306, "Chords"),
                     (17, 28912, "Bass")]
fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-58s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)


def item(schema):
    return B.read_item(os.path.join(FIX, "init-%s.mfpz" % VERSIONS[schema]))


def fields(wire):
    return [("%s/%s" % (m, n), v) for m, n, v in T.fields(T.decode_7to8(list(wire)))]


def with_engine(wire, count, value):
    dec = T.decode_7to8(list(wire))
    groups = [(m, [(n, bytes([count, value & 0xFF, value >> 8])
                    if (m, n) == ("VCO", "Type") else v) for n, v in rows])
              for m, rows in T.groups(dec)]
    stream = T.build(groups)
    return T.encode_7to8(stream + dec[len(stream):])[:len(wire)]


def test_formats():
    print("\nevery older Init converts to each newer format's own Init")
    for target in C.ORDER:
        want = item(target).wire()
        same, _ = C.convert(want, target, target)
        chk(same == want, "%s: a %s preset is left alone" % (C.label(target), C.label(target)))
        for source in C.ORDER[:C.ORDER.index(target)]:
            old = item(source)
            out, notes = C.convert(old.wire(), source, target)
            got, ref = fields(out), dict(fields(want))
            src = dict(fields(old.wire()))
            # Defaults that changed between the two firmwares: the old
            # preset's own values, which conversion keeps.
            expect = {k for k, v in src.items() if k in ref and ref[k] != v
                      and k != "VCO/Type"}
            differ = {k for k, v in got if ref.get(k) != v}
            ok = ([k for k, _ in got] == [k for k, _ in fields(want)]
                  and differ == expect and (expect or out == want))
            chk(ok, "%s -> %s" % (C.label(source), C.label(target)),
                ("byte-identical" if out == want else "differs in " + ", ".join(sorted(differ))))
        for newer in C.ORDER[C.ORDER.index(target) + 1:]:
            try:
                C.convert(item(newer).wire(), newer, target)
                chk(False, "%s -> %s is refused" % (C.label(newer), C.label(target)))
            except C.ConvertError:
                pass
    chk(True, "converting to an older format is refused")


def test_engines():
    print("\nthe engine is the one the instrument showed")
    base = item(0x10).wire()
    for count, value, engine in ON_THE_INSTRUMENT:
        out, notes = C.convert(with_engine(base, count, value))
        chk(notes[0] == "engine " + engine, "%2d engines, value %5d -> %s"
            % (count, value, engine), notes[0])
    out, notes = C.convert(with_engine(base, 12, 16384), target=0x32)
    vco = dict(fields(out))["VCO/Type"]
    chk(vco[0] == 18 and vco[1] | vco[2] << 8 == C.engine_value(5, 18),
        "for 4.0 it is stored against 4.0's 18 engines", vco.hex())
    try:
        C.convert(with_engine(base, 30, 1000))
        chk(False, "an engine count no release had is refused")
    except C.ConvertError:
        chk(True, "an engine count no release had is refused")


class FakeSynth:
    """Answers the MIDI identity request as firmware `version` would."""

    def __init__(self, version):
        self.version, self.q = version, []

    def send(self, msg):
        if self.version and list(msg) == C.INQUIRY:
            major, minor, patch = self.version
            self.q.append([0xF0, 0x7E, 0x7F, 0x06, 0x02, 0x00, 0x20, 0x6B, 0x06,
                           0x00, 0x06, 0x01, major, minor, patch, 0x00, 0xF7])

    def recv(self, predicate, timeout=None):
        for m in self.q:
            if predicate(m):
                return m
        raise TimeoutError("no reply")


def test_instrument():
    print("\nwrite and restore convert for the instrument's own firmware")
    chk([C.schema_for_firmware(*v) for v in ((1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (4, 0), (5, 0))]
        == [0x10, 0x10, 0x11, 0x30, 0x31, 0x32, 0x33], "each release's format")
    old = item(0x10)
    for version, schema in (((4, 0, 3), 0x32), ((5, 0, 0), 0x33)):
        (sent,), notes = C.to_send(FakeSynth(version), [old])
        chk(C.source_format(sent.wire()) == schema and sent.archive.schema == schema,
            "a 1.x preset sent to %d.%d.%d is converted to its format" % version,
            notes[0][:60] if notes else "")
    (sent,), notes = C.to_send(FakeSynth((1, 0, 5)), [old])
    chk(sent is old and not notes, "a 1.x preset sent to 1.0.5 goes as stored")
    try:
        C.to_send(FakeSynth((4, 0, 3)), [item(0x33)])
        chk(False, "a 5.0 preset sent to 4.0.3 is refused")
    except C.ConvertError as exc:
        chk("--no-convert" in str(exc), "a 5.0 preset sent to 4.0.3 is refused", str(exc)[:50])
    refused = []
    for version in ((1, 0, 5), (4, 0, 3), (6, 0, 0)):
        try:
            C.check_protocol(FakeSynth(version)); refused.append(False)
        except C.ConvertError:
            refused.append(True)
    try:
        C.check_protocol(FakeSynth((5, 0, 0))); C.check_protocol(FakeSynth(None)); ok5 = True
    except C.ConvertError:
        ok5 = False
    chk(all(refused) and ok5, "write and restore refuse firmware other than 5.0")
    for version, why in ((None, "does not report"), ((6, 0, 0), "no public release")):
        (sent,), notes = C.to_send(FakeSynth(version), [old])
        chk(sent is old and why in notes[0], "an instrument that %s gets it as stored" % why)


def test_files(work):
    print("\nfiles")
    old = item(0x32)
    ar, notes = C.convert_item(old)
    chk(ar.schema == C.CURRENT_SCHEMA and ar.name == old.name
        and ar.tags == old.tags and ar.scalars == old.archive.scalars,
        "the archive keeps its name, Type and tags; its schema is the target's")
    bank = os.path.join(work, "old.mfprojz")
    B.write_bank(bank, [(1, item(0x10).archive), (2, item(0x33).archive)],
                 top="old", subdir="01-old-A", ext="mbp")
    for to, schema in (("5.0", 0x33), ("4.0", 0x32)):
        out = os.path.join(work, "new-%s.mfprojz" % to)
        sys.argv = ["mfreak preset convert", bank, out, "--to", to]
        try:
            code = C.main()
        except SystemExit as exc:
            code = exc.code
        back = list(B.read_bank(out)) if os.path.exists(out) else []
        if to == "5.0":
            chk(code == 0 and all(C.source_format(i.wire()) == 0x33 for i in back),
                "a bank converts for 5.0", code)
        else:
            chk(code == 1 and len(back) == 1 and C.source_format(back[0].wire()) == 0x32,
                "for 4.0 the 1.x preset converts and the 5.0 one is refused", code)


def main():
    print(__doc__.splitlines()[0])
    with tempfile.TemporaryDirectory() as work:
        test_formats()
        test_engines()
        test_instrument()
        test_files(work)
    print("\n%s" % ("FAIL - %d check(s) failed" % len(fails) if fails
                    else "PASS - older presets convert to every newer format"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
