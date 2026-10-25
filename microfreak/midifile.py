"""Read a Standard MIDI File into a flat, timed list of MIDI byte messages.

What comes out is `[(seconds, bytes)]`, sorted, with running status expanded
so every message carries its own status byte. `mfreak play` and
`mfreak render` send that list to the instrument in real time.

This is the same parser as microfreak-emu's `mfemu.midifile`, which
feeds the identical list into the emulated MIDI port -- so a file played
here and rendered there is the same stream of bytes at the same times.
`from_notes` and `standard_phrase` are copied too, so `--notes` and
`--phrase` mean the same thing to both renders. It is a copy rather than an
import because microfreak-emu depends on this
package, never the reverse. Dependency-free on purpose: reading a file
format should not need the MIDI backend.

Channel-voice messages are kept, meta events are consumed for their tempo and
then dropped, and SysEx is passed through as raw bytes. A file whose division
is SMPTE rather than ticks-per-quarter is refused rather than guessed at.
"""
import struct
import sys

DEFAULT_TEMPO = 500000          # microseconds per quarter note, i.e. 120 bpm


class MidiFileError(Exception):
    pass


def _varint(data, i):
    """SMF variable-length quantity -> (value, next_index)."""
    value = 0
    for _ in range(4):
        if i >= len(data):
            raise MidiFileError("truncated variable-length quantity")
        b = data[i]
        i += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, i
    raise MidiFileError("variable-length quantity longer than 4 bytes")


def _chunks(data):
    """Yield (tag, body) for each top-level SMF chunk."""
    i = 0
    while i + 8 <= len(data):
        tag = data[i:i + 4]
        (length,) = struct.unpack(">I", data[i + 4:i + 8])
        body = data[i + 8:i + 8 + length]
        if len(body) != length:
            raise MidiFileError("chunk %r claims %d bytes, %d present"
                                % (tag, length, len(body)))
        yield tag, body
        i += 8 + length


def _events(track):
    """Parse one MTrk body -> [(tick, status, payload_bytes)].

    Running status is expanded here.  Meta events keep their 0xFF status and
    their type byte in the payload so the tempo map can be built from them;
    they are dropped later.
    """
    out = []
    i = 0
    tick = 0
    status = None
    while i < len(track):
        delta, i = _varint(track, i)
        tick += delta
        if i >= len(track):
            break
        b = track[i]
        if b & 0x80:
            status = b
            i += 1
        elif status is None:
            raise MidiFileError("running status before any status byte")
        if status == 0xFF:                       # meta
            kind = track[i]
            i += 1
            length, i = _varint(track, i)
            out.append((tick, 0xFF, bytes([kind]) + track[i:i + length]))
            i += length
            status = None                        # meta cancels running status
        elif status in (0xF0, 0xF7):             # SysEx, possibly split
            length, i = _varint(track, i)
            out.append((tick, status, track[i:i + length]))
            i += length
            status = None                        # as does SysEx
        else:
            n = 1 if (status & 0xF0) in (0xC0, 0xD0) else 2
            out.append((tick, status, track[i:i + n]))
            i += n
    return out


def read(path, channel=None, include_sysex=True):
    """-> [(seconds, bytes)] for every message worth sending to the device.

    `channel` (0-15) keeps only that channel's voice messages; None keeps all.
    The MicroFreak is one instrument, so a multi-channel file rendered whole
    will stack every part onto it -- usually you want a channel.
    """
    data = open(path, "rb").read()
    header = None
    tracks = []
    for tag, body in _chunks(data):
        if tag == b"MThd":
            header = struct.unpack(">HHh", body[:6])
        elif tag == b"MTrk":
            tracks.append(_events(body))
    if header is None:
        raise MidiFileError("%s: not a Standard MIDI File (no MThd chunk)" % path)
    fmt, ntrks, division = header
    if division <= 0:
        raise MidiFileError("%s: SMPTE time division (%d) is not supported"
                            % (path, division))

    # Format 1 shares one timeline across tracks, so merge first and let the
    # tempo map apply to the merged stream.  Format 2's tracks are independent
    # songs; treating them as one timeline would be wrong, so refuse.
    if fmt == 2 and len(tracks) > 1:
        raise MidiFileError("%s: format 2 files hold independent sequences; "
                            "extract one track first" % path)

    merged = []
    for track in tracks:
        merged.extend(track)
    merged.sort(key=lambda e: e[0])

    # Walk once, converting ticks to seconds as the tempo changes.  Tempo is
    # microseconds per quarter note and a file may change it repeatedly.
    out = []
    tempo = DEFAULT_TEMPO
    last_tick = 0
    seconds = 0.0
    for tick, status, payload in merged:
        seconds += (tick - last_tick) * tempo / (division * 1e6)
        last_tick = tick
        if status == 0xFF:
            if payload[0] == 0x51 and len(payload) >= 4:
                tempo = int.from_bytes(payload[1:4], "big")
            continue
        if status in (0xF0, 0xF7):
            if include_sysex:
                # 0xF0 events carry their own terminating 0xF7 in the payload.
                out.append((seconds, bytes([status]) + payload))
            continue
        if channel is not None and (status & 0x0F) != channel:
            continue
        out.append((seconds, bytes([status]) + payload))
    return out


# The generation-2 render protocol. One definition, because the feature
# extractor has to know where the held note ends and the sequence begins --
# if the renderer and the analyser disagree about that, every rhythm and
# chaos feature is computed over the wrong samples.
#
# A single held note cannot express rhythm, and nothing that evolves over more
# than a couple of seconds survives a 2 s clip. The four notes are spread
# across the keyboard so key tracking and glide show up, deliberately not a
# triad so nothing sounds musical in a way that flatters a candidate, and
# there are enough note-ons to wake an arpeggiator or sequencer.
PHRASE_HOLD_NOTE = 48                    # C3
PHRASE_HOLD_DUR = 2.5
PHRASE_GAP_AFTER_HOLD = 0.2
PHRASE_SEQ = (48, 55, 63, 58)            # C3 G3 D#4 A#3
PHRASE_SEQ_DUR = 0.3
PHRASE_SEQ_GAP = 0.1
PHRASE_LAST_HOLD = 1.0
PHRASE_TAIL = 1.0


def standard_phrase(velocity=100, channel=0):
    """-> (events, segments) for the generation-2 protocol.

    `segments` names the spans in seconds so an analyser can slice the render
    into the sustained part and the sequence without re-deriving the timing.
    """
    events = []
    t = 0.0
    events.append((t, bytes([0x90 | channel, PHRASE_HOLD_NOTE, velocity])))
    events.append((t + PHRASE_HOLD_DUR, bytes([0x80 | channel, PHRASE_HOLD_NOTE, 0x40])))
    hold_span = (0.0, PHRASE_HOLD_DUR)

    t = PHRASE_HOLD_DUR + PHRASE_GAP_AFTER_HOLD
    seq_start = t
    for i, note in enumerate(PHRASE_SEQ):
        dur = PHRASE_LAST_HOLD if i == len(PHRASE_SEQ) - 1 else PHRASE_SEQ_DUR
        events.append((t, bytes([0x90 | channel, note, velocity])))
        events.append((t + dur, bytes([0x80 | channel, note, 0x40])))
        t += dur + PHRASE_SEQ_GAP
    seq_span = (seq_start, t - PHRASE_SEQ_GAP)

    events.sort(key=lambda e: e[0])
    segments = {"hold": hold_span, "sequence": seq_span,
                "tail": (seq_span[1], seq_span[1] + PHRASE_TAIL),
                "total": (0.0, seq_span[1] + PHRASE_TAIL)}
    return events, segments


def from_notes(spec, velocity=100, channel=0, gap=0.0, hold=False):
    """A convenience alternative to a file: "60,67" or "60:1.5,67:0.5".

    Each note runs for its duration (default 1 s) and is released at the end
    unless `hold` is set, which is how you audition a chord.  Kept here rather
    than in the CLI so both entry points produce the same shape of data.
    """
    events = []
    t = 0.0
    pending = []
    for item in [s.strip() for s in spec.split(",") if s.strip()]:
        note, _, dur = item.partition(":")
        note = int(note, 0)
        dur = float(dur) if dur else 1.0
        events.append((t, bytes([0x90 | channel, note, velocity])))
        if hold:
            pending.append(note)
        else:
            events.append((t + dur, bytes([0x80 | channel, note, 0x40])))
        t += dur + gap
    for note in pending:
        events.append((t, bytes([0x80 | channel, note, 0x40])))
    events.sort(key=lambda e: e[0])
    return events
