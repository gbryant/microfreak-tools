#!/usr/bin/env python3
"""Play a MIDI file on the instrument in real time.

    mfreak play ode_to_joy.mid
    mfreak play ode_to_joy.mid --preset 385
    mfreak play song.mid --channel 2 --preset 72

This is the hardware side of rendering a MIDI file with `mfemu render`: the
same file, sent the same way, so the two can be compared.

Everything in the file is sent except meta events. The instrument listens on
one channel, Channel 1 by default, so a file on another channel plays
nothing. --channel keeps one channel's messages but does not move them to
the instrument's channel.

Timing is close to a render's but not sample-accurate, because of USB MIDI
latency. Every held note is released at the end, on Ctrl-C and on error.
"""
import argparse
import os
import sys
import time

from microfreak import jsonout
from microfreak import midifile
from microfreak import panel
from microfreak.midi import open_synth
from microfreak.preset.select import select

# Time between selecting a preset and the first note. The firmware loads the
# page from external flash after the Program Change; a note sent inside that
# window can land on the previous preset. Generous, because nobody minds a
# quarter second before a tune and a wrong first note looks like a bug.
PRESET_SETTLE = 0.25
SPIN = 0.010


def schedule(events, synth, held, clock=time.perf_counter, sleep=time.sleep):
    """Send `[(seconds, bytes)]` on time, keeping `held` current.

    Each event is sent at its offset from the first call, against an absolute
    deadline, so a slow send delays one message rather than everything after
    it. `held` is the caller's set of (channel, note) and is updated as notes
    go on and off, so whatever is still down when this is interrupted is
    exactly what the caller has to release.
    """
    start = clock()
    for when, msg in events:
        due = start + when
        # Sleep to just short of the deadline, then spin the rest. A plain
        # sleep overshoots by up to ~5 ms on macOS, measured, which is
        # audible as a lazy rhythm. 2 ms of spin was not enough -- events still
        # landed 3 ms late -- so it is 10 ms. With it, measured 2026-10-20 on 20
        # events of Ode to Joy, the worst send was 17 us off its deadline. The
        # cost is a core spinning 10 ms per event: ~5% of one for that tune.
        wait = due - clock() - SPIN
        if wait > 0:
            sleep(wait)
        while clock() < due:
            pass
        synth.send(list(msg))
        kind, chan = msg[0] & 0xF0, msg[0] & 0x0F
        if kind == 0x90 and len(msg) >= 3 and msg[2] > 0:
            held.add((chan, msg[1]))
        elif kind in (0x80, 0x90) and len(msg) >= 2:
            held.discard((chan, msg[1]))


def release(synth, held, channels):
    """Note-off for everything in `held`, then All Notes Off on `channels`."""
    for chan, note in sorted(held):
        synth.send([0x80 | chan, note, 0])
    for chan in sorted(channels):
        synth.send([0xB0 | chan, 123, 0])


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.splitlines()[1:]))
    ap.add_argument("file", metavar="FILE.mid", help="a Standard MIDI File")
    ap.add_argument("--preset", type=int, metavar="N",
                    help="select this preset first (1-%d)" % panel.PRESETS)
    ap.add_argument("--channel", type=int, metavar="N",
                    help="play only this MIDI channel of the file, 1-16")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    args = ap.parse_args()

    # Everything that can be wrong with the request is checked BEFORE a port
    # is opened, so a typo never reaches the instrument.
    slot = panel.to_slot(ap, args.preset, panel.PRESETS, "preset")
    if args.channel is not None and not 1 <= args.channel <= 16:
        ap.error("--channel must be 1-16. Got %d." % args.channel)
    chan = None if args.channel is None else args.channel - 1
    if not os.path.isfile(args.file):
        sys.exit("%s: no such file" % args.file)
    try:
        events = midifile.read(args.file, channel=chan)
    except midifile.MidiFileError as exc:
        sys.exit("%s: not a playable MIDI file -- %s" % (args.file, exc))
    if not events:
        sys.exit("%s: no MIDI messages to play%s"
                 % (args.file, "" if chan is None
                    else " on channel %d" % args.channel))
    notes = sum(1 for _, m in events if m[0] & 0xF0 == 0x90 and len(m) > 2
                and m[2] > 0)
    channels = {m[0] & 0x0F for _, m in events if m[0] < 0xF0}
    length = events[-1][0]

    synth = open_synth(args.port)
    name = getattr(synth, "port_name", "the emulator")
    held = set()
    interrupted = False
    try:
        if slot is not None:
            bank, program = select(synth, slot)
            print("preset %d -> bank %d, program %d" % (slot + 1, bank, program))
            time.sleep(PRESET_SETTLE)
        print("playing %s: %d notes, %.1f s, on %s"
              % (os.path.basename(args.file), notes, length, name))
        try:
            schedule(events, synth, held)
        except KeyboardInterrupt:
            interrupted = True
            print("\nstopped")
    finally:
        release(synth, held, channels or {0})
        synth.close()
    jsonout.result(file=os.path.abspath(args.file), notes=notes,
                   messages=len(events), seconds=round(length, 3),
                   channels=sorted(c + 1 for c in channels),
                   preset=None if slot is None else slot + 1,
                   port=name, interrupted=interrupted)
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
