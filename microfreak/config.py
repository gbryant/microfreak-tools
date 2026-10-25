#!/usr/bin/env python3
"""Read and write the instrument's global settings, as on MCC's DEVICE page.

    mfreak config read                      every setting, as a table
    mfreak config read --id "Knob Catch"    one setting
    mfreak config read --out settings.json  save them to edit or restore

    mfreak config write "Knob Catch" Scaled --yes
    mfreak config write --from settings.json --yes

The saved file uses MCC's names and values, so you can edit it by hand:

    "MIDI Output Destination": "Both",
    "Keyboard sensitivity": "60 %",

`config write --from` writes only the settings that differ, and refuses the
whole file if any line will not parse. The instrument does not acknowledge
writes, so each one is read back to confirm it.

The instrument answers 46 setting ids. 31 match a control on MCC's page; the
other 15 are unidentified, but are saved and restored too.
"""

# How the protocol and the setting table were established.
#
# This is the exchange MIDI Control Center opens every session with, before it
# touches a single preset:
#
#     ->  F0 00 20 6B 07 01 <seq> 01 43 <id> F7
#     <-  F0 00 20 6B 07 7F <nn>  02 42 <id> <value> F7
#
# MCC sends 31 of these. **The device answers all 46 ids from 0x20 to 0x4D**,
# measured on 5.0.0.2084, so this reads the whole range rather than only the
# ids MCC happens to ask for.
#
# A WRITE is the reply's own form, sent the other way:
#
#     ->  F0 00 20 6B 07 01 <seq> 02 42 <id> <value> F7
#
# `07 01` is the host's header and `07 7F` the device's, so the only difference
# between MCC's write and the device's answer is which end it came from. **The
# device sends nothing back**: there is no ack in the capture, and the only way
# to confirm a write is to read the id again, which `config write` does.
#
# **The reply does not echo the sequence byte we sent.** It carries the
# device's own counter, which steps by 2 (`0x5B, 0x5D, 0x5F, ...`). The preset
# name request DOES echo ours, so a matcher written from that habit matches
# almost nothing here. Replies are therefore matched on the id, which the device does echo.
#
# **The ids are labelled, from a capture.** A USB capture (not in this repo)
# recorded MCC's DEVICE page being worked through control by control -- each
# setting moved off its value and put straight back -- top to bottom of the
# left column, then top to bottom of the right. It holds exactly 31 write
# pairs for the page's exactly 31 controls, so the Nth pair is the Nth
# control, and `SETTINGS` below is that correspondence.
#
# Four things make it more than an assumed alignment:
#
#   * **Every restored value matches a live read.** All 31 second-halves equal
#     what `config read` returns from the instrument today. 31/31.
#   * **Two rows confirm their own position.** `CV 0V Reference` = C1 reads 36
#     and `CV 1V Reference` = C0 reads 24 -- MIDI note numbers, an octave
#     apart, which is only true if those two rows landed where they did.
#   * **Three more read as their own units**: both MIDI channels show "1" and
#     hold 0, and `Master Tuning` at 0 cent holds 64, the centre of 0-127.
#   * **The counts are exact.** 62 writes, 31 pairs, 31 controls, no remainder.
#
# **The values are decoded too, from a second capture.** That one walked
# every dropdown from the top, restoring each setting before moving on, with a
# screenshot of each open list. The Nth selection is the Nth label, so the
# numbers pair with MCC's own wording rather than with a guess. `VALUES`
# below holds the result, and `config read` prints it. Checked end to end: all
# 31 decoded labels equal what MCC displays for this instrument's current
# settings, 31/31.
#
# Three of them are not indexes at all, which is why guessing would have
# failed:
#
#   * `MIDI Output Destination` is a BITMASK -- USB is bit 0, MIDI is bit 2,
#     so Both is 1|4 = 5. There is no value 2 or 3.
#   * `MIDI Merge` is a mask too, over USB and MIDI, with KBD in every option.
#   * `Sync Clock In/Out` is 1-BASED. There is no value 0, and 24ppq is 3.
#
# And three are arithmetic rather than lists: `Keyboard sensitivity` shows
# value + 10 percent (so 50 is "60 %", which is what the owner said it was),
# `Mic Gain` shows value - 12 dB with 72 meaning Auto Gain, and `Noise Gate`
# shows -(28 + 2*value) dB with 0 meaning Off.
#
# **MCC's wording is not the firmware's.** The payload's own string table says
# `2PPQ` where MCC says `1pulse (Korg)`, `Penta` where MCC says `Pentatonic`,
# and `Int` where MCC says `Internal`. The labels here are MCC's, read off its
# dropdowns, because MCC's page is what a reader is looking at.
#
# `MIDI Input Channel` carries two sentinels above the channel range: `All`
# reads 127 and `None` reads 126, both confirmed by setting them and reading
# back. `MIDI Output Channel` has neither -- its dropdown is 1 to 16 and
# nothing else. `Global tempo` is Off or On, owner-confirmed.
#
# **The id table is confirmed twice, independently.** The firmware's own menu
# records pair a label with a descriptor whose low byte is the config id --
# `Knob Catch` with 0x2D, `Cent Offset` with 0x42, `KBD Sensitivity` with
# 0x41, and so on. Extracted from the 5.0.0.2084 payload at base 0x0800C000,
# those records cover exactly the same 31 ids this capture produced, with
# matching names. The instrument's own wording differs in places and MCC's is
# used here, since MCC's page is what a reader is looking at.
#
# **Fifteen ids answer and have no control on the page**: 0x22, 0x23, 0x29,
# 0x2A, 0x2C, 0x2F, 0x30, 0x32, 0x33, 0x34, 0x35, 0x40, 0x43, 0x48, 0x4B.
# 31 + 15 = the 46 that answer. What they are is unknown.

import argparse
import datetime
import json
import re
import sys

from microfreak.midi import open_synth, build, checksum_free_seq
from microfreak import confirm
from microfreak import jsonout

FIRST_ID, LAST_ID = 0x20, 0x4D
REPLY_LEN = 12

# id -> the label MCC's DEVICE page gives it, in that page's own order:
# the left column top to bottom, then the right. Derived as the docstring
# describes; the ids are not contiguous and not in page order.
SETTINGS = {
    0x20: "MIDI Input Channel",
    0x21: "MIDI Output Channel",
    0x25: "MIDI Output Destination",
    0x26: "Local control",
    0x2B: "Arp/Seq MIDI OUT",
    0x3B: "MIDI through",
    0x24: "Knob send CCs",
    0x3C: "MIDI Merge",
    0x2E: "MIDI clock source",
    0x31: "Sync Clock In/Out Settings",
    0x3D: "Global tempo",
    0x38: "CV Pitch Format",
    0x39: "CV Gate Format",
    0x3A: "CV Press Range",
    0x36: "CV 0V Reference",          # a MIDI note number: 36 is C1
    0x37: "CV 1V Reference",          # a MIDI note number: 24 is C0
    0x2D: "Knob Catch",
    0x3E: "Click to Load Preset",
    0x4C: "Osc Knob Speed",
    0x4D: "Oct LED Blink",
    0x42: "Master Tuning",            # 64 is 0 cent
    0x3F: "Memory protection",
    0x41: "Keyboard sensitivity",
    0x27: "Aftertouch curve",
    0x28: "Velocity curve",
    0x44: "Relative Bend",
    0x45: "Scale",
    0x46: "Root Note",
    0x47: "Mic Gain",
    0x49: "Noise Gate",
    0x4A: "Mic Detection",
}

# The order MCC's page shows them in, which is not the id order.
PAGE_ORDER = [0x20, 0x21, 0x25, 0x26, 0x2B, 0x3B, 0x24, 0x3C, 0x2E, 0x31,
              0x3D, 0x38, 0x39, 0x3A, 0x36, 0x37,
              0x2D, 0x3E, 0x4C, 0x4D, 0x42, 0x3F, 0x41, 0x27, 0x28, 0x44,
              0x45, 0x46, 0x47, 0x49, 0x4A]


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _in_channel(v):
    # The two ends of the dropdown are sentinels above the channel range,
    # not indexes into it: 'All' is the first entry and reads 127, 'None' is
    # the last and reads 126. Channels are plain 0-based in between.
    if v == 127:
        return "All"
    if v == 126:
        return "None"
    return str(v + 1) if v < 16 else None


def _out_channel(v):
    return str(v + 1) if v < 16 else None


def _note(v):
    # MCC calls MIDI note 36 'C1', so the octave is v//12 - 2.
    return "%s%d" % (NOTE_NAMES[v % 12], v // 12 - 2) if v < 128 else None


def _cents(v):
    return "0 cent" if v == 64 else "%+d cent" % (v - 64)


def _sensitivity(v):
    return "%d %%" % (v + 10) if v <= 90 else None


def _mic_gain(v):
    return "Auto Gain" if v == 72 else ("%d dB" % (v - 12) if v < 72 else None)


def _noise_gate(v):
    return "Off" if v == 0 else ("-%d dB" % (28 + 2 * v) if v <= 31 else None)


def _press_range(v):
    return "%d V" % (v + 1) if v < 8 else None


ON_OFF = {0: "Off", 1: "On"}
CURVE = {0: "Lin", 1: "Log", 2: "Exp"}

# What each value means, where it is known. Labels are MCC's own, read off
# its dropdowns -- NOT the firmware's string table, whose wording differs:
# MCC's '1pulse (Korg)' is the firmware's '2PPQ', its 'Pentatonic' is
# 'Penta', and its 'Internal' is 'Int'.
VALUES = {
    0x20: _in_channel,
    0x21: _out_channel,
    # Not an index but a BITMASK: USB is bit 0, MIDI is bit 2, Both is 1|4.
    0x25: {0: "None", 1: "USB", 4: "MIDI", 5: "Both"},
    0x26: ON_OFF,
    0x2B: ON_OFF,
    0x3B: ON_OFF,
    0x24: ON_OFF,
    # Also a mask, and KBD is in every option: USB is bit 0, MIDI is bit 1.
    0x3C: {1: "USB+KBD", 2: "MIDI+KBD", 3: "MIDI+USB+KBD"},
    0x2E: {0: "Internal", 1: "USB", 2: "MIDI", 3: "Clock", 4: "Auto"},
    # 1-based: there is no value 0 here.
    0x31: {1: "1step (Clock)", 2: "1pulse (Korg)", 3: "24ppq", 4: "48ppq"},
    0x3D: ON_OFF,
    0x38: {0: "1V/Oct", 1: "Hz/V", 2: "1.2V/Oct"},
    0x39: {0: "S-trig", 1: "V-trig 5V", 2: "V-trig 12V"},
    0x3A: _press_range,
    0x36: _note,
    0x37: _note,
    0x2D: {0: "Jump", 1: "Hook", 2: "Scaled"},
    0x3E: ON_OFF,
    0x4C: {0: "Slow", 1: "Fast"},
    0x4D: ON_OFF,
    0x42: _cents,
    0x3F: {0: "OFF", 1: "Factory only", 2: "All"},
    0x41: _sensitivity,
    0x27: CURVE,
    0x28: CURVE,
    0x44: ON_OFF,
    0x45: {0: "OFF", 1: "Major", 2: "Minor", 3: "HarmoMinor", 4: "Dorian",
           5: "Mixolydian", 6: "Blues", 7: "Pentatonic"},
    0x46: lambda v: NOTE_NAMES[v] if v < 12 else None,
    0x47: _mic_gain,
    0x49: _noise_gate,
    0x4A: ON_OFF,
}


def shown(cid, value):
    """-> what MCC would display for this value, or None if not known."""
    if value is None:
        return None
    table = VALUES.get(cid)
    if table is None:
        return None
    if callable(table):
        try:
            return table(value)
        except Exception:
            return None
    return table.get(value)


def _note_value(text):
    """'C1' -> 36. The inverse of _note, including the odd spellings."""
    m = re.match(r"^([A-Ga-g])(#|b)?(-?\d+)$", text.strip())
    if not m:
        return None
    base = NOTE_NAMES.index(m.group(1).upper())
    if m.group(2) == "#":
        base += 1
    elif m.group(2) == "b":
        base -= 1
    v = base + (int(m.group(3)) + 2) * 12
    return v if 0 <= v <= 127 else None


def _number_in(text, unit):
    """Pull the number out of '60 %', '-70 dB', '5 V', '+3 cent'."""
    m = re.match(r"^([+-]?\d+)\s*%s$" % re.escape(unit), text.strip(),
                 re.IGNORECASE)
    return int(m.group(1)) if m else None


# The inverse of each computed decoder, so a dump written in MCC's own words
# can be read back. Each one is checked against its forward function over
# every value 0-127 by tests/test_config_roundtrip.py -- the pair has to be
# exact or an edited file would write something other than what it says.
def _parse_in_channel(t):
    t = t.strip()
    if t.lower() == "all":
        return 127
    if t.lower() == "none":
        return 126
    return int(t) - 1 if t.isdigit() and 1 <= int(t) <= 16 else None


def _parse_out_channel(t):
    t = t.strip()
    return int(t) - 1 if t.isdigit() and 1 <= int(t) <= 16 else None


def _parse_cents(t):
    n = _number_in(t, "cent")
    return n + 64 if n is not None and -64 <= n <= 63 else None


def _parse_sensitivity(t):
    n = _number_in(t, "%")
    return n - 10 if n is not None and 10 <= n <= 100 else None


def _parse_mic_gain(t):
    if t.strip().lower() == "auto gain":
        return 72
    n = _number_in(t, "dB")
    return n + 12 if n is not None and -12 <= n <= 59 else None


def _parse_noise_gate(t):
    if t.strip().lower() == "off":
        return 0
    n = _number_in(t, "dB")
    if n is None or n > 0 or (-n - 28) % 2:
        return None
    v = (-n - 28) // 2
    return v if 1 <= v <= 31 else None


def _parse_press_range(t):
    n = _number_in(t, "V")
    return n - 1 if n is not None and 1 <= n <= 8 else None


PARSERS = {
    0x20: _parse_in_channel, 0x21: _parse_out_channel,
    0x36: _note_value, 0x37: _note_value,
    0x3A: _parse_press_range, 0x41: _parse_sensitivity,
    0x42: _parse_cents, 0x47: _parse_mic_gain, 0x49: _parse_noise_gate,
    0x46: lambda t: (NOTE_NAMES.index(t.strip().upper())
                     if t.strip().upper() in NOTE_NAMES else None),
}


# What to say when a hand-edited value will not parse. A dict-valued
# setting can list its own options; these cannot, and "try a number 0-127"
# is no help at all when the thing you typed was "-71 dB".
HINTS = {
    0x20: "All, None, or a channel 1-16",
    0x21: "a channel 1-16",
    0x36: "a note name like C1 (MIDI note 0-127)",
    0x37: "a note name like C0 (MIDI note 0-127)",
    0x3A: "1 V to 8 V",
    0x41: "10 % to 100 %",
    0x42: "-64 cent to +63 cent",
    0x46: "a note name, C to B",
    0x47: "Auto Gain, or -12 dB to 59 dB",
    0x49: "Off, or -30 dB to -90 dB in 2 dB steps",
}


def accepts(cid):
    """-> a phrase naming what this setting will take."""
    table = VALUES.get(cid)
    if isinstance(table, dict):
        return ", ".join(sorted(table.values()))
    return HINTS.get(cid, "a number 0-127")


def value_of(cid, text):
    """-> the number for a label like 'Scaled' or '-70 dB', else None.

    The inverse of `shown()`. A plain number is always accepted too, so a
    hand-edited file can say either.
    """
    text = str(text).strip()
    # The LABEL is tried first, and that ordering is load-bearing: the MIDI
    # channel labels are themselves digits, so reading "3" as the raw value 3
    # would set channel 4. What the file says is what MCC shows, and only a
    # string MCC would never show falls through to being a raw number.
    table = VALUES.get(cid)
    if isinstance(table, dict):
        want = text.lower()
        for v, name in table.items():
            if name.lower() == want:
                return v
    else:
        parse = PARSERS.get(cid)
        got = parse(text) if parse else None
        if got is not None:
            return got
    try:
        n = int(text, 0)
    except ValueError:
        return None
    if not 0 <= n <= 127:
        return None
    # A raw number is accepted only where it means something: for a setting
    # we can decode, that is a value with a label. Without this, "17" for a
    # MIDI channel -- which has no channel 17 -- quietly became the raw value
    # 17, which is channel 18. Ids with no table take any byte, since there
    # is nothing to check them against.
    if cid in VALUES and shown(cid, n) is None:
        return None
    return n


def label(cid):
    """-> the setting's name, or a marker for the ids with no control."""
    return SETTINGS.get(cid, "(no control on MCC's page)")


def write_message(cid, value, seq):
    """-> the SysEx bytes MCC sends to set one setting.

    Checked against `captures/device-settings.pcapng`: this reproduces all 62
    of that session's frames byte for byte, given each frame's own seq.
    """
    return build(seq, 0x02, 0x42, cid, value)


def read_id(synth, cid, seqgen):
    """-> the value byte for one config id, or None if it does not answer.

    Matched on the id, not on our sequence number -- see the module docstring.
    """
    synth.send(build(next(seqgen), 0x01, 0x43, cid))
    try:
        reply = synth.recv(lambda m: len(m) == REPLY_LEN and m[7] == 0x02
                           and m[8] == 0x42 and m[9] == cid)
    except TimeoutError:
        return None
    return reply[10]


def read_all(synth, ids, seqgen):
    """-> [(id, value)], value None where the id did not answer."""
    out = []
    for cid in ids:
        # Drain first: an earlier id's late reply would otherwise still be
        # queued and would match this request, since matching is by id and
        # not by seq -- the device does not echo ours here.
        synth.drain()
        out.append((cid, read_id(synth, cid, seqgen)))
    return out


def an_id(text):
    """A setting, by number (0x2D or 45) or by the name MCC gives it."""
    try:
        return int(text, 0)
    except ValueError:
        pass
    want = text.strip().lower()
    hits = [c for c, n in SETTINGS.items() if n.lower() == want]
    if not hits:
        hits = [c for c, n in SETTINGS.items() if want in n.lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise argparse.ArgumentTypeError(
            "%r matches %s -- be more specific"
            % (text, ", ".join(sorted(SETTINGS[c] for c in hits))))
    raise argparse.ArgumentTypeError(
        "%r is neither a number nor one of: %s"
        % (text, ", ".join(SETTINGS[c] for c in PAGE_ORDER)))


def write_dump(path, rows):
    """Write a settings file a person can edit.

    Keyed by MCC's own names and valued with MCC's own words -- `"Knob
    Catch": "Jump"` -- because the point of the file is that someone opens
    it, changes a line and writes it back. The first version of this keyed
    on `"0x2D"` and valued `0`, which is a faithful record and useless to
    edit.

    The 15 ids with no control on MCC's page keep their hex id, under a
    separate heading so they are clearly not settings anyone named. They are
    written out and read back unchanged, so a dump still restores the whole
    device and not just the part that is understood.
    """
    named, other = {}, {}
    for cid in PAGE_ORDER:
        value = dict(rows).get(cid)
        if value is None:
            continue
        named[SETTINGS[cid]] = shown(cid, value)
        if named[SETTINGS[cid]] is None:       # a value outside the table
            named[SETTINGS[cid]] = value
    for cid, value in rows:
        if cid not in SETTINGS and value is not None:
            other["0x%02X" % cid] = value
    with open(path, "w") as fh:
        json.dump({"tool": "mfreak config read",
                   "device": "MicroFreak",
                   "saved": datetime.datetime.now().astimezone()
                   .isoformat(timespec="seconds"),
                   "settings": named,
                   "unidentified": other}, fh, indent=1)
        fh.write("\n")
    return path


def read_dump(path):
    """-> [(id, value)] from a settings file. Raises SystemExit on nonsense.

    Accepts both shapes: the readable one `write_dump` produces, and the
    flat `{"0x20": 0}` an earlier version wrote, so old dumps still restore.
    """
    with open(path) as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        sys.exit("%s is not a settings file" % path)
    groups = [doc.get("settings", {}), doc.get("unidentified", {})]
    if not any(groups):
        groups = [doc]                 # the oldest shape: the bare mapping
    out, bad = [], []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for key, value in group.items():
            if key in ("tool", "device", "saved"):
                continue
            cid = None
            try:
                cid = int(key, 0)
            except (TypeError, ValueError):
                for c, name in SETTINGS.items():
                    if name.lower() == str(key).strip().lower():
                        cid = c
                        break
            if cid is None:
                bad.append("%r is not a setting" % key)
                continue
            # A JSON NUMBER is a raw value; a JSON STRING must be a label.
            # That split is what lets `write_dump` record a value it cannot
            # label -- a firmware with an option we have not enumerated --
            # and still read it back, without reopening the hole where the
            # string "17" became the raw value 17 on a MIDI channel.
            if isinstance(value, bool) or not isinstance(value, int):
                got = value_of(cid, value)
            else:
                got = value if 0 <= value <= 127 else None
            if got is None:
                bad.append("%s: %r is not a value -- try %s"
                           % (key, value, accepts(cid)))
                continue
            out.append((cid, got))
    if bad:
        sys.exit("%s:\n  %s" % (path, "\n  ".join(bad)))
    return sorted(set(out))


def _read_parser(sub):
    ap = sub.add_parser("read", description=__doc__,
                        help="dump the settings",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", type=an_id, action="append", metavar="N",
                    help="read only this setting (repeatable); a number like "
                         "0x35, or a name like \"Knob Catch\"")
    ap.add_argument("--out", metavar="FILE",
                    help="write the settings here as JSON you can edit, and "
                         "put back with `config write --from`")
    return ap


def _write_parser(sub):
    ap = sub.add_parser(
        "write", help="change a setting, or put a whole dump back",
        description="Change one global setting, or restore a `config read "
                    "--out` dump.\n\n"
                    "    mfreak config write \"Knob Catch\" Scaled --yes\n"
                    "    mfreak config write --from settings.json --yes\n\n"
                    "It shows what it would change and asks first. --yes skips "
                    "the question.\nEach write is read back to confirm it. Save first with `mfreak "
                    "config read --out FILE` to be able to undo.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("setting", nargs="?", type=an_id, metavar="SETTING",
                    help="a number like 0x2D, or a name like \"Knob Catch\"")
    ap.add_argument("value", nargs="?",
                    help="0-127, or the label MCC shows, e.g. \"Scaled\"")
    ap.add_argument("--from", dest="restore", metavar="FILE",
                    help="restore a `config read --out` file")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip reading each id back afterwards")
    ap.add_argument("--yes", action="store_true",
                    help="write without asking")
    return ap


def _common(ap):
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=2.0,
                    help="seconds to wait per reply (default: 2)")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for build_one in (_read_parser, _write_parser):
        _common(build_one(sub))
    args = ap.parse_args()
    return do_write(args) if args.cmd == "write" else do_read(args)


def do_read(args):
    ids = args.id if args.id else list(range(FIRST_ID, LAST_ID + 1))
    for cid in ids:
        if not 0 <= cid <= 0x7F:
            sys.exit("ids are single SysEx data bytes: 0x00-0x7F")

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        rows = read_all(synth, ids, seqgen)
    finally:
        synth.close()

    answered = [(c, v) for c, v in rows if v is not None]
    quiet = [c for c, v in rows if v is None]

    print("id     value  setting                       shows as")
    for cid, value in rows:
        print("0x%02X   %5s  %-28s  %s"
              % (cid, "--" if value is None else value, label(cid),
                 shown(cid, value) or ""))
    print("\n%d of %d ids answered%s"
          % (len(answered), len(rows),
             "; no reply from %s" % " ".join("0x%02X" % c for c in quiet)
             if quiet else ""))
    print("%d of these are named; the rest are saved and restored too."
          % sum(1 for c, _ in rows if c in SETTINGS))

    if args.out:
        write_dump(args.out, rows)
        print("wrote %s. Edit it, then: mfreak config write --from %s --yes"
              % (args.out, args.out))

    jsonout.result(first=ids[0], last=ids[-1], answered=len(answered),
                   no_reply=quiet, out=args.out,
                   settings=[dict(id=c, hex="0x%02X" % c, value=v,
                                  name=SETTINGS.get(c), shows_as=shown(c, v))
                             for c, v in rows])
    return 0 if answered else 1


def _wanted(args):
    """-> [(id, value)] the user asked to set."""
    if args.restore:
        return read_dump(args.restore)
    if args.setting is None or args.value is None:
        sys.exit("give a setting and a value, or --from FILE")
    got = value_of(args.setting, args.value)
    if got is None:
        sys.exit("%r is not a value for %s -- try %s"
                 % (args.value, label(args.setting), accepts(args.setting)))
    return [(args.setting, got)]


def do_write(args):
    pairs = _wanted(args)
    for cid, value in pairs:
        if not 0 <= cid <= 0x7F or not 0 <= value <= 127:
            sys.exit("id and value are single SysEx data bytes: 0x00-0x7F")

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    changed, unchanged, failed = [], [], []
    try:
        # Read first: a write is only worth sending where the value differs,
        # and the before/after is the only evidence the write did anything.
        before = {cid: read_id(synth, cid, seqgen) for cid, _ in pairs}
        todo = [(c, v) for c, v in pairs if before.get(c) != v]
        unchanged = [(c, v) for c, v in pairs if before.get(c) == v]

        print("%d setting(s) asked for, %d already at that value"
              % (len(pairs), len(unchanged)))
        for cid, value in todo:
            was, now = before.get(cid), value
            print("  0x%02X  %-28s %s -> %s"
                  % (cid, label(cid),
                     "%s (%s)" % (was, shown(cid, was)) if shown(cid, was)
                     else was,
                     "%s (%s)" % (now, shown(cid, now)) if shown(cid, now)
                     else now))
        if not confirm.ask("Change %d setting%s?"
                           % (len(todo), "" if len(todo) == 1 else "s"), args.yes):
            jsonout.result(dry_run=True, would_write=len(todo),
                           already_set=len(unchanged))
            return 0

        for cid, value in todo:
            synth.drain()
            synth.send(write_message(cid, value, next(seqgen)))
            if args.no_verify:
                changed.append(cid)
                continue
            got = read_id(synth, cid, seqgen)
            (changed if got == value else failed).append(cid)
            if got != value:
                print("  0x%02X  %s: asked %s, reads back %s"
                      % (cid, label(cid), value, got))
    finally:
        synth.close()

    print("\n%d written%s%s"
          % (len(changed),
             ", %d unchanged" % len(unchanged) if unchanged else "",
             ", %d DID NOT TAKE" % len(failed) if failed else ""))
    if changed and not args.no_verify:
        print("each one read back as the value asked for.")
    # `verified` is the read-back VERDICT everywhere else in this tool --
    # True means it matched, False means it did not, None means nobody
    # looked. It used to be `not args.no_verify`, i.e. "did we check", which
    # emitted {"exit": 1, "verified": true, "failed": ["0x05"]}: a caller
    # branching on `verified` was told a failed write had been confirmed.
    jsonout.result(written=len(changed), already_set=len(unchanged),
                   failed=["0x%02X" % c for c in failed],
                   verified=None if args.no_verify else not failed)
    return 1 if failed else 0



if __name__ == "__main__":
    sys.exit(main())
