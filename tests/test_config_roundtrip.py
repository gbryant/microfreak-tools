#!/usr/bin/env python3
"""Settings decode and encode must be exact inverses.

    uv run python tests/test_config_roundtrip.py

No instrument: it is all table and arithmetic. Under a second.

`mfreak config read --out FILE` writes what MCC would show -- `"Knob Catch":
"Jump"` -- so that someone can open the file, change a line and put it back
with `config write --from`. That only works if every label maps back to the
number it came from, for every value, with no exceptions. A near-miss here
does not fail loudly; it writes a different setting than the file says.

Two bugs this caught while it was being written, both of the same shape:

  * **The MIDI channel labels are digits.** Reading the value before the
    label made `"3"` the raw value 3, which is channel 4. The label has to
    win, and that ordering is now checked.
  * **A raw number was accepted anywhere.** `"MIDI Input Channel": "17"` --
    a channel that does not exist -- fell through to the raw value 17, which
    is channel 18. A bare number is now accepted only where it decodes to
    something, so 17 is refused and 0 is still channel 1.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import microfreak.config as C          # noqa: E402

fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-52s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)


def test_every_label_round_trips():
    print("\n1. every label, every value")
    bad, checked = [], 0
    for cid in sorted(set(C.VALUES) | set(C.SETTINGS)):
        for v in range(128):
            label = C.shown(cid, v)
            if label is None:
                continue
            checked += 1
            if C.value_of(cid, label) != v:
                bad.append("0x%02X %r -> %r -> %r"
                           % (cid, v, label, C.value_of(cid, label)))
    chk(not bad, "shown() then value_of() returns the same number",
        "; ".join(bad[:4]) or "%d labels over %d settings"
        % (checked, len(C.SETTINGS)))


def test_labels_beat_numbers():
    print("\n2. a label that looks like a number")
    # The channel lists are the only labels that are bare digits, and they
    # are off by one from the stored value, so this is where it shows.
    chk(C.value_of(0x20, "3") == 2, "'3' on a MIDI channel means channel 3",
        "-> %r" % C.value_of(0x20, "3"))
    chk(C.value_of(0x21, "16") == 15, "'16' means channel 16",
        "-> %r" % C.value_of(0x21, "16"))
    chk(C.value_of(0x20, "All") == 127 and C.value_of(0x20, "None") == 126,
        "'All' is 127 and 'None' is 126")


def test_impossible_values_are_refused():
    print("\n3. values that cannot exist")
    cases = [(0x20, "17", "a 17th MIDI channel"),
             (0x2D, "9", "a 10th Knob Catch"),
             (0x49, "-71 dB", "an odd-numbered noise gate"),
             (0x41, "5 %", "sensitivity below the 10 % floor"),
             (0x47, "60 dB", "mic gain past 59 dB"),
             (0x36, "H3", "a note that is not a note")]
    bad = ["0x%02X %r (%s)" % (c, t, why) for c, t, why in cases
           if C.value_of(c, t) is not None]
    chk(not bad, "a value outside the setting's range is refused",
        "; ".join(bad) or "%d cases" % len(cases))
    # ...but an id with no decoder still takes any byte, or a dump of the
    # 15 unidentified settings could not be restored.
    chk(C.value_of(0x22, "99") == 99, "an unidentified id still takes any byte")


def test_file_round_trip():
    print("\n4. the file")
    state = [(cid, v) for cid, v in
             [(0x20, 0), (0x25, 5), (0x31, 3), (0x41, 50), (0x47, 72),
              (0x49, 21), (0x36, 36), (0x42, 64), (0x22, 1), (0x35, 126)]]
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "s.json")
        C.write_dump(p, state)
        doc = json.load(open(p))
        chk(doc["settings"]["MIDI Output Destination"] == "Both"
            and doc["settings"]["Noise Gate"] == "-70 dB",
            "the file is written in MCC's own words",
            "%d named, %d unidentified"
            % (len(doc["settings"]), len(doc["unidentified"])))
        chk(C.read_dump(p) == sorted(state),
            "reading it back gives the same device state")

        # A hand edit is the whole point of the format.
        doc["settings"]["Knob Catch"] = "Scaled"
        json.dump(doc, open(p, "w"))
        chk(dict(C.read_dump(p))[0x2D] == 2, "a hand-edited label is picked up")

        # A value we cannot label -- a firmware with an option this build
        # does not know -- is written as a bare number and must still load,
        # or the dump would not restore the device it came from.
        C.write_dump(p, [(0x2D, 5), (0x20, 0)])
        chk(json.load(open(p))["settings"]["Knob Catch"] == 5
            and C.read_dump(p) == [(0x20, 0), (0x2D, 5)],
            "an unlabelled value survives the round trip as a number")
        doc = json.load(open(p))
        doc["settings"]["MIDI Input Channel"] = "17"
        json.dump(doc, open(p, "w"))
        try:
            C.read_dump(p)
            ok = False
        except SystemExit:
            ok = True
        chk(ok, "...without letting the string '17' back in as a raw value")

        # And the older flat shape still loads, so old dumps still restore.
        json.dump({"settings": {"0x2D": 1, "0x20": 0}}, open(p, "w"))
        chk(C.read_dump(p) == [(0x20, 0), (0x2D, 1)],
            "the earlier flat format still loads")


def main():
    print(__doc__.splitlines()[0])
    for t in (test_every_label_round_trips, test_labels_beat_numbers,
              test_impossible_values_are_refused, test_file_round_trip):
        t()
    print("\n%s" % ("FAIL - %d check(s) failed" % len(fails) if fails
                    else "PASS - decode and encode are exact inverses"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
