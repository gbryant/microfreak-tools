"""The MIDI transport: port discovery, framing, and request/reply.

Everything that talks to the instrument goes through `Synth`. The SysEx frame
is Arturia's, documented from captures of MIDI Control Center's own traffic:

    F0 00 20 6B 07 01 <seq> <cmd> <sub> <payload...> F7

`seq` is a free-running 7-bit counter -- the device echoes it, which is how a
reply is matched to its request rather than to whatever arrived first.

`rtmidi2` is imported inside `Synth.__init__`, not at module level. It is a
compiled extension and builds from source when no wheel matches the
interpreter, so a broken install of it must not take down the pure functions
here -- `build` and `checksum_free_seq` -- which the preset, sample and
wavetable modules all import without ever opening a port.

The backend is rtmidi2, not python-rtmidi. Both bind the same RtMidi C++
library; rtmidi2 is the one still publishing wheels. Four API differences
matter and all of them are silent if missed: ports are a `.ports` property
rather than `get_ports()`, the callback is an attribute rather than
`set_callback()`, `ignore_types` takes `midi_sysex`/`midi_time`/`midi_sense`,
and `get_message()` returns a flat list of ints where python-rtmidi returned
`(message, deltatime)`.
"""
import os
import queue
import time

EMULATOR_ENV = "MFREAK_EMULATOR"


def open_synth(port_substring="MicroFreak", timeout=6.0):
    """-> a `Synth` on a real port, or the EMULATOR if MFREAK_EMULATOR is set.

    Every command that talks to an instrument goes through here, so the whole
    tool can be pointed at `mfemu serve` without any command knowing about it:

        mfemu serve --preset 7=Speech          # in one shell, leave it up
        MFREAK_EMULATOR=1 mfreak preset read 7 # in another

    Set the variable to `1` for the default session, or to a session file path
    if you are running more than one machine.

    **The import is deliberately lazy.** microfreak-emu depends on this
    package, not the other way round, so importing it at module level would
    make the dependency circular and would make this package unusable without
    the emulator installed. Nothing is imported until someone asks for it.

    The timeout is raised for the emulator because it is slower than the
    instrument: a preset read is 146 request/reply round trips, and each one
    waits on a machine running under emulation.
    """
    spec = os.environ.get(EMULATOR_ENV, "").strip()
    if not spec:
        return Synth(port_substring=port_substring, timeout=timeout)
    try:
        from mfemu.midilink import EmulatedSynth
    except ImportError as exc:
        raise SystemExit(
            "%s is set, but microfreak-emu is not installed here (%s).\n"
            "  Run from the microfreak-emu checkout:\n"
            "      cd ../microfreak-emu && MFREAK_EMULATOR=1 uv run mfreak ...\n"
            "  Or unset %s to talk to a real instrument."
            % (EMULATOR_ENV, exc, EMULATOR_ENV))
    session = None if spec.lower() in ("1", "yes", "true", "auto") else spec
    synth = EmulatedSynth.attach(session=session, timeout=max(timeout, 60.0))
    print("talking to the EMULATOR, not an instrument (%s set)" % EMULATOR_ENV)
    return synth


MANUFACTURER = [0x00, 0x20, 0x6B]


def checksum_free_seq():
    n = 0
    while True:
        yield n
        n = (n + 1) % 128


def build(seq, cmd, sub, *payload):
    return [0xF0, *MANUFACTURER, 0x07, 0x01, seq, cmd, sub, *payload, 0xF7]


class Synth:
    # 6.0, not 2.0: the device can stall for several seconds right after a
    # large transfer (a 91-block sample write, a 4-frame wavetable write)
    # before it answers the next request. At 2.0 the first call after one of
    # those intermittently raised TimeoutError and succeeded on a plain retry.
    def __init__(self, port_substring="MicroFreak", timeout=6.0):
        import rtmidi2
        self.timeout = timeout
        self.inbox = queue.Queue()
        self.midi_in = rtmidi2.MidiIn()
        self.midi_out = rtmidi2.MidiOut()
        self.midi_in.ignore_types(midi_sysex=False, midi_time=True,
                                  midi_sense=True)

        in_port = self._find(self.midi_in.ports, port_substring)
        out_port = self._find(self.midi_out.ports, port_substring)
        # Kept so a command can say which port it used without re-enumerating
        # and without opening rtmidi2 itself, which is how `preset select`
        # came to bypass this class entirely.
        self.port_name = self.midi_out.ports[out_port]
        self.midi_in.open_port(in_port)
        self.midi_out.open_port(out_port)
        # Opened first, then the callback: a port with no callback queues into
        # rtmidi2's 1024-byte ring, and a preset read is 146 replies.
        self.midi_in.callback = self._on_message

    @staticmethod
    def _find(ports, substring):
        for i, name in enumerate(ports):
            if substring.lower() in name.lower():
                return i
        raise SystemExit(f"no MIDI port matching {substring!r} found. "
                          f"Available: {ports}")

    def _on_message(self, message, _timestamp=None):
        # rtmidi2 hands the callback a flat list of ints. python-rtmidi handed
        # it `(message, deltatime)`, and unpacking the wrong one here does not
        # raise -- it fills the inbox with nonsense -- so it is spelled out.
        self.inbox.put(bytes(message))

    def send(self, message):
        self.midi_out.send_message(message)

    def recv(self, predicate, timeout=None):
        """Waits for a SysEx message matching predicate(bytes) -> bool."""
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no matching reply from synth")
            try:
                msg = self.inbox.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError("no matching reply from synth")
            if predicate(msg):
                return msg

    def drain(self):
        """Discard anything already waiting. -> how many were dropped.

        Part of the transport interface, not an inbox detail: `EmulatedSynth`
        implements it too. A command that matches replies by CONTENT rather
        than by sequence number -- `config read` has to, because the device
        answers config requests with its own seq, not ours -- must start each
        request from an empty queue, or a late reply to the previous request
        is the thing that matches.
        """
        dropped = 0
        while not self.inbox.empty():
            try:
                self.inbox.get_nowait()
            except queue.Empty:
                break
            dropped += 1
        return dropped

    def close(self):
        self.midi_in.close_port()
        self.midi_out.close_port()

