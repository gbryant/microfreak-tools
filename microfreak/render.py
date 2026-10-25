#!/usr/bin/env python3
"""Play notes or a MIDI file on the instrument and record it to a WAV.

    mfreak render --preset 319 --midi ode_to_joy.mid --out hw.wav
    mfreak render --preset 319 --notes 60,67 --out buzz.wav
    mfreak render --list-devices

The instrument's audio output must be connected to an audio interface.
--device picks the input by name or number. The default is the system's
default input. Numbers change when a device connects or disconnects,
so a name is safer.

The flags match `mfemu render`, so the same command renders the emulator's
version for comparison:

    mfemu render --preset 319 --midi ode_to_joy.mid --out emu.wav

The file starts when the first note is sent and runs --tail seconds past the
last. The output is mono, folded from both input channels, unless --stereo.
A render that drops audio is still written, and exits 1.
"""
import argparse
import os
import sys
import time

from microfreak import audio
from microfreak import jsonout
from microfreak import midifile
from microfreak import panel
from microfreak.midi import EMULATOR_ENV, open_synth
from microfreak.play import PRESET_SETTLE, release, schedule
from microfreak.preset.select import select

# Below this the recording is treated as silence: nothing reached the input.
SILENT_DBFS = -60.0


def build_events(ap, args):
    """-> the event list for --midi, --notes or --phrase. Exits on bad input."""
    if sum(bool(v) for v in (args.midi, args.notes, args.phrase)) > 1:
        ap.error("--midi, --notes and --phrase are alternatives")
    if args.channel is not None and not 1 <= args.channel <= 16:
        ap.error("--channel must be 1-16. Got %d." % args.channel)
    if not 1 <= args.velocity <= 127:
        ap.error("--velocity must be 1-127. Got %d." % args.velocity)
    if args.phrase:
        return midifile.standard_phrase(velocity=args.velocity)[0]
    if args.midi:
        if not os.path.isfile(args.midi):
            sys.exit("%s: no such file" % args.midi)
        chan = None if args.channel is None else args.channel - 1
        try:
            events = midifile.read(args.midi, channel=chan)
        except midifile.MidiFileError as exc:
            sys.exit("%s: not a playable MIDI file. %s" % (args.midi, exc))
        if not events:
            sys.exit("%s: no MIDI messages to play" % args.midi)
        return events
    try:
        return midifile.from_notes(args.notes or "60", velocity=args.velocity,
                                   hold=args.hold)
    except ValueError:
        ap.error('--notes takes notes like "60,67" or "60:1.5,67:0.5"')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.splitlines()[1:]))
    ap.add_argument("--preset", type=int, metavar="N",
                    help="select this preset first (1-%d)" % panel.PRESETS)
    ap.add_argument("--midi", metavar="FILE.mid", help="a MIDI file to play")
    ap.add_argument("--notes", metavar="SPEC",
                    help='notes to play, e.g. "60,67" or "60:1.5,67:0.5" '
                         '(note:seconds). The default is one second of 60')
    ap.add_argument("--hold", action="store_true",
                    help="with --notes, hold every note to the end")
    ap.add_argument("--phrase", action="store_true",
                    help="play the standard test phrase `mfemu render` uses")
    ap.add_argument("--velocity", type=int, default=100,
                    help="note velocity (default: 100)")
    ap.add_argument("--channel", type=int, metavar="N",
                    help="with --midi, play only this channel of the file, 1-16")
    ap.add_argument("--out", metavar="FILE.wav", help="the file to write")
    ap.add_argument("--tail", type=float, default=1.0,
                    help="seconds to keep recording after the last note "
                         "(default: 1)")
    rec = ap.add_argument_group("recording")
    rec.add_argument("--device", metavar="NAME|N",
                     help="audio input, by name or number (default: the "
                          "system default)")
    rec.add_argument("--rate", type=int, default=audio.DEFAULT_RATE,
                     help="sample rate (default: %d)" % audio.DEFAULT_RATE)
    rec.add_argument("--stereo", action="store_true",
                     help="keep both input channels instead of folding to mono")
    rec.add_argument("--list-devices", action="store_true",
                     help="list audio inputs and stop")
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    args = ap.parse_args()

    if args.list_devices:
        try:
            devs = audio.inputs()
        except audio.AudioError as exc:
            sys.exit(str(exc))
        print(audio.describe(devs))
        jsonout.result(devices=[dict(index=i, name=n, channels=c, rate=r)
                                for i, n, c, r in devs])
        return 0

    # Everything about the request is checked before a port or a device is
    # opened, so a typo never reaches the instrument.
    if not args.out:
        ap.error("--out is required")
    slot = panel.to_slot(ap, args.preset, panel.PRESETS, "preset")
    if args.tail < 0:
        ap.error("--tail cannot be negative")
    events = build_events(ap, args)
    if os.environ.get(EMULATOR_ENV, "").strip():
        sys.exit("render records the instrument's audio output. For the "
                 "emulator, use `mfemu render`.")
    try:
        audio.find_input(args.device)
    except audio.AudioError as exc:
        sys.exit(str(exc))

    channels = {m[0] & 0x0F for _, m in events if m[0] < 0xF0}
    notes = sum(1 for _, m in events if m[0] & 0xF0 == 0x90 and len(m) > 2
                and m[2] > 0)
    length = events[-1][0] + args.tail

    synth = open_synth(args.port)
    held = set()
    interrupted = False
    try:
        if slot is not None:
            bank, program = select(synth, slot)
            print("preset %d -> bank %d, program %d" % (slot + 1, bank, program))
            time.sleep(PRESET_SETTLE)
        try:
            recorder = audio.Recorder(args.device, args.rate)
            with recorder:
                print("recording from %s: %d notes, %.1f s"
                      % (recorder.name, notes, length))
                start = recorder.mark()
                try:
                    schedule(events, synth, held)
                    release(synth, held, channels or {0})
                    time.sleep(args.tail)
                    recorder.wait_for(start + int(round(length * args.rate)))
                except KeyboardInterrupt:
                    interrupted = True
                    print("\nstopped")
        except audio.AudioError as exc:
            sys.exit(str(exc))
    finally:
        release(synth, held, channels or {0})
        synth.close()

    x = recorder.audio()[start:start + int(round(length * args.rate))]
    if not args.stereo:
        x = audio.mono(x)
    audio.write_wav(args.out, x, args.rate)
    peak, clipped = audio.levels(x)
    dropped = recorder.dropped()
    seconds = len(x) / float(args.rate)
    first = audio.onset(x, args.rate)

    print("%s  %.2f s  %d Hz  %s  peak %.1f dBFS  first sound at %s"
          % (args.out, seconds, args.rate, "stereo" if args.stereo else "mono",
             peak, "-" if first is None else "%.0f ms" % (first * 1000)))
    if clipped:
        print("warning: %d samples clipped. Turn the interface's input gain "
              "down." % clipped, file=sys.stderr)
    if peak < SILENT_DBFS:
        print("warning: the recording is silent. Check the cable, the "
              "interface's input, and --device.", file=sys.stderr)
    gaps = [(at - start / float(args.rate), lost) for at, lost in recorder.gaps]
    failed = bool(recorder.overflows or dropped)
    if failed:
        print("error: the recording dropped audio (%d overflows, %.3f s "
              "missing). The file has gaps." % (recorder.overflows, dropped),
              file=sys.stderr)
        for at, lost in gaps:
            print("  %.0f ms missing at %.2f s" % (lost * 1000, at),
                  file=sys.stderr)
    jsonout.result(out=os.path.abspath(args.out), seconds=round(seconds, 3),
                   rate=args.rate, channels=2 if args.stereo else 1,
                   device=recorder.name, notes=notes,
                   preset=None if slot is None else slot + 1,
                   peak_dbfs=None if peak == float("-inf") else round(peak, 1),
                   onset_seconds=None if first is None else round(first, 4),
                   clipped=clipped, overflows=recorder.overflows,
                   dropped_seconds=round(dropped, 3),
                   gaps=[dict(at=round(at, 3), seconds=round(lost, 3))
                         for at, lost in gaps],
                   interrupted=interrupted)
    if interrupted:
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
