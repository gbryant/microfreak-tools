#!/usr/bin/env python3
"""Find out why something isn't working, and how to fix it.

    mfreak doctor              check this machine and the instrument
    mfreak doctor --offline    check this machine only

Checks are grouped by what they block, and each failure names its fix. The
instrument counts as present only if it answers a Device Inquiry. Installed
plugins add their own checks, such as whether the instrument runs their
firmware; those only read.

Exits 0 when everything needed offline is present, and 1 when something is
missing. An unplugged instrument is not a failure.
"""
import argparse
import shutil
import sys
import time

from microfreak import jsonout
from microfreak import plugin
from microfreak.firmware.peek import INQUIRY_REPLY_PREFIX, INQUIRY_REQUEST

PAD = 24


class Report:
    """Collects lines, remembers whether anything is actually broken."""

    def __init__(self):
        self.broken = []
        # Every check, in order, for --json. The prose below is unchanged.
        self.checks = []
        self._tier = None

    def tier(self, title, blocks):
        self._tier = title
        print("\n%s" % title)
        print("  %s" % blocks)

    def _record(self, status, label, value, **extra):
        self.checks.append(dict(tier=self._tier, status=status, check=label,
                                value=str(value), **extra))

    def ok(self, label, value):
        self._record("ok", label, value)
        print("    %-*s %s" % (PAD, label, value))

    def note(self, label, value):
        self._record("note", label, value)
        print("    %-*s %s" % (PAD, label, value))

    def bad(self, label, value, blocks=None, fix=None, fatal=True):
        self._record("fail" if fatal else "warn", label, value,
                     blocks=blocks,
                     fix=(fix if isinstance(fix, list) else [fix]) if fix else [])
        print("    %-*s %s" % (PAD, label, value))
        if blocks:
            print("    %-*s   blocks: %s" % (PAD, "", blocks))
        if fix:
            for i, line in enumerate(fix if isinstance(fix, list) else [fix]):
                print("    %-*s   %s %s" % (PAD, "", "fix:" if i == 0 else "    ", line))
        if fatal:
            self.broken.append(label)


def tier_offline(r):
    r.tier("offline -- no synth, no firmware image needed",
           "preset encode, sample encode, wavetable encode")
    v = sys.version_info
    r.ok("python", "%d.%d.%d" % (v.major, v.minor, v.micro))
    try:
        import numpy
        r.ok("numpy", numpy.__version__)
    except ImportError:
        r.bad("numpy", "MISSING", blocks="everything that analyses audio",
              fix="reinstall: uv tool install microfreak-tools")
    p = shutil.which("ffmpeg")
    if p:
        r.ok("ffmpeg", p)
    else:
        r.bad("ffmpeg", "MISSING",
              blocks="reading audio files: sample encode, wavetable encode",
              fix="brew install ffmpeg")
    from microfreak import audio
    try:
        import sounddevice
        n = len(audio.inputs())
        r.ok("audio input", "%s, %d input(s)"
             % (sounddevice.get_portaudio_version()[1].split(",")[0], n))
    except (audio.AudioError, ImportError, OSError) as exc:
        r.bad("audio input", "unavailable (%s)" % exc,
              blocks="render, and recording",
              fix="on Linux: install libportaudio2", fatal=False)
    for p in _hooks("offline"):
        p(r)


def _hooks(name):
    """-> each installed plugin's doctor function `name`."""
    out = []
    for p in plugin.installed().values():
        if not p.doctor:
            continue
        try:
            mod = __import__(p.doctor, fromlist=["_"])
        except Exception as exc:
            print("    %-*s did not load: %s" % (PAD, p.name, exc))
            continue
        if hasattr(mod, name):
            out.append(getattr(mod, name))
    return out


def _open_midi(port_substring):
    """-> (midi_out, midi_in, port_name) or (None, None, reason)."""
    try:
        import rtmidi2
    except ImportError as e:
        return None, None, "rtmidi2 did not import (%s)" % e
    try:
        mo, mi = rtmidi2.MidiOut(), rtmidi2.MidiIn()
    except Exception as e:
        return None, None, "rtmidi2 failed to start (%s)" % e
    # Each list is searched SEPARATELY. rtmidi2 enumerates inputs and outputs
    # independently, so the same instrument need not sit at the same index in
    # both: one device present in only one list -- a controller keyboard, a
    # virtual port -- shifts the other. This used to find the index in
    # `mo.ports` and then open `mi` at that index, which is how `doctor` could
    # report "answers inquiry: NO" for a perfectly healthy instrument, i.e.
    # the diagnostic tool inventing the fault it exists to find.
    #
    # Case-insensitive, matching midi.Synth._find.
    def find(ports):
        return next((i for i, n in enumerate(ports)
                     if port_substring.lower() in n.lower()), None)

    out_names, in_names = mo.ports, mi.ports
    o, i = find(out_names), find(in_names)
    if o is None or i is None:
        missing = "output" if o is None else "input"
        seen = out_names if o is None else in_names
        return None, None, ("no MIDI %s matching %r (saw: %s)"
                            % (missing, port_substring,
                               ", ".join(seen) or "nothing"))
    try:
        mo.open_port(o)
        mi.open_port(i)
    except Exception as e:
        return None, None, ("port %r exists but will not open (%s)"
                            % (out_names[o], e))
    mi.ignore_types(midi_sysex=False, midi_time=True, midi_sense=True)
    return mo, mi, out_names[o]


def _inquiry(mo, mi, wait=2.0, tries=3):
    """-> the Identity Reply as a plugin.Inquiry, or None. A reply that looks
    stock is followed by the WHO_OPCODE request a patched build answers."""
    for _ in range(tries):
        while mi.get_message():
            pass
        mo.send_message(list(INQUIRY_REQUEST))
        end = time.time() + wait
        while time.time() < end:
            m = mi.get_message()
            if m and bytes(m).startswith(INQUIRY_REPLY_PREFIX):
                reply = bytes(m)
                return plugin.answered(reply, _who(mo, mi) if plugin.looks_stock(reply)
                                       else None)
            time.sleep(0.005)
    return None


def _who(mo, mi, wait=0.5, tries=2):
    """-> (letter, rev) if the instrument answers WHO_OPCODE, else None."""
    from microfreak.firmware.peek import who_message, who_reply
    for _ in range(tries):
        mo.send_message(list(who_message(0x5A)))
        end = time.time() + wait
        while time.time() < end:
            m = mi.get_message()
            got = who_reply(bytes(m), 0x5A) if m else None
            if got:
                return got
            time.sleep(0.005)
    return None


def _describe_identity(reply):
    """Standard MIDI Identity Reply, as the MicroFreak answers it.

    F0 7E 7F 06 02 | 00 20 6B | ff ff | mm mm | rr rr rr rr | F7
                     maker      family  member  revision

    The revision is major.minor.patch, then the build number cut to 7 bits:
    0x24 for 2084. A patched build answers as stock and names itself through
    WHO_OPCODE; older ones put a letter in the patch number and their revision
    in the build. See microfreak.firmware.patching.
    """
    if len(reply) < 16:
        return "answered, but the reply is %d bytes (expected 17)" % len(reply)
    maker = " ".join("%02X" % b for b in reply[5:8])
    rev = reply[12:16]
    what, build = plugin.running(reply)
    who = getattr(reply, "who", None)
    if who:
        return ("firmware 5.0.0 (build byte 0x%02X), maker %s; names itself %s, rev %d"
                % (rev[3], maker, who[0], who[1]))
    if build is not None:
        name = " (%s)" % what if what else ""
        return ("firmware 5.0.%s, build %d%s, maker %s"
                % (chr(rev[2]), build, name, maker))
    return ("firmware %d.%d.%d (build byte 0x%02X), maker %s"
            % (rev[0], rev[1], rev[2], rev[3], maker))


def _describe_running(reply):
    """-> which firmware the inquiry reply names."""
    what, rev = plugin.running(reply)
    if what == "stock":
        return "stock firmware, or a patch that does not report itself"
    if what == "official":
        return "official firmware %d.%d.%d, which fw patch does not apply to" % tuple(reply[12:15])
    if what is None:
        who = getattr(reply, "who", None)
        return "a patched build no installed plugin claims (%s, rev %d)" % (
            who[0] if who else "5.0." + chr(reply[14]), rev)
    if what == "words":
        return "stock firmware with your own words, from `speech embed` (rev %d)" % rev
    note = "" if what in plugin.installed() else ", plugin not installed"
    return "the %s patch, rev %d%s" % (what, rev, note)


def firmware_version(port_substring="MicroFreak", wait=2.0, tries=3):
    """-> the instrument's firmware as `doctor` reports it, or None if it
    does not answer over MIDI."""
    mo, mi, _ = _open_midi(port_substring)
    if mo is None:
        return None
    try:
        reply = _inquiry(mo, mi, wait=wait, tries=tries)
    finally:
        try:
            mo.close_port(); mi.close_port()
        except Exception:
            pass
    return _describe_identity(reply) if reply else None


def tier_device(r, port_substring, args):
    r.tier("the instrument", "preset, sample, wavetable, config")
    mo, mi, name = _open_midi(port_substring)
    if mo is None:
        # `name` carries the reason when the port could not be opened.
        # A missing backend is not a cable problem, and telling someone to
        # check their cable when the real fault is an uninstalled module sends
        # them to the wrong place entirely.
        fix = ("install the MIDI backend: `pip install rtmidi2`, or reinstall "
               "microfreak-tools, which depends on it"
               if "did not import" in name else
               "check the USB cable, and quit MIDI Control Center if it is open")
        r.bad("MIDI port", "NOT AVAILABLE -- %s" % name,
              blocks="every command that talks to the instrument",
              fix=fix, fatal=False)
        return
    r.ok("MIDI port", name)
    try:
        reply = _inquiry(mo, mi)
        if reply is None:
            r.bad("answers inquiry", "NO -- the port is open but nothing replies",
                  blocks="every command that talks to the instrument",
                  fix=["another program may hold the port (MIDI Control Center)",
                       "or the instrument is in the bootloader: power-cycle it"],
                  fatal=False)
            return
        r.ok("answers inquiry", _describe_identity(reply))
        r.ok("running", _describe_running(reply))

        hooks = _hooks("device")
        if not hooks:
            return
        if args.no_probe:
            r.note("plugin checks", "not run (--no-probe)")
            return
        for check in hooks:
            check(r, mo, mi, args, reply)

        # The checks only read, but prove the instrument survived them.
        if _inquiry(mo, mi, wait=1.5, tries=2) is None:
            r.bad("STILL RESPONDING", "NO -- it answered before the plugin "
                  "checks and not after", blocks="everything; power-cycle the "
                  "instrument", fix="please report this: the checks are meant "
                  "to change nothing")
    finally:
        try:
            mo.close_port(); mi.close_port()
        except Exception:
            pass


def tier_flash(r):
    r.tier("flashing -- WRITES FLASH, can brick the instrument", "fw flash")
    import microfreak.firmware.flash as F
    try:
        import usb.core
    except ImportError as e:
        r.bad("pyusb", "MISSING (%s)" % e, blocks="fw flash",
              fix="reinstall: uv tool install microfreak-tools")
        return
    r.ok("pyusb", "ok")
    # Importing pyusb needs no backend; finding a device does. That split is
    # why libusb is not a hard requirement for the rest of the tool.
    try:
        dev = usb.core.find(idVendor=F.VENDOR, idProduct=F.PRODUCT,
                            backend=F.backend())
    except Exception as e:
        r.bad("libusb backend", "NOT USABLE (%s)" % e, blocks="fw flash",
              fix=["libusb-package should supply it -- reinstall "
                   "microfreak-tools, which depends on it",
                   "or install a system one: brew install libusb"])
        return
    # Say WHICH libusb, because "ok" hid the thing most worth knowing: whether
    # this machine is relying on a system library it may not have.
    r.ok("libusb backend", F.backend_label())
    if dev is None:
        r.note("USB device", "not found (%04X:%04X). Connect it by USB, not "
                             "only MIDI DIN" % (F.VENDOR, F.PRODUCT))
        return
    r.ok("USB device", "%04X:%04X present" % (F.VENDOR, F.PRODUCT))
    # The same VID/PID answers in both modes, and BOTH configurations expose a
    # DFU interface -- that is what makes the 0xF2 reboot reachable from a
    # running instrument, and it is why the presence of that interface says
    # nothing about the mode. The interface COUNT is the real signal: 3 for the
    # application (DFU + AudioControl + MIDIStreaming), 1 for the bootloader.
    try:
        boot = F.in_bootloader(dev)
        n = dev[0].bNumInterfaces
    except Exception as e:
        r.note("mode", "could not be read (%s)" % e)
        return
    if boot:
        r.ok("mode", "BOOTLOADER (DFU only, %d interface) -- ready to flash" % n)
    else:
        r.ok("mode", "application (%d interfaces) -- `mfreak fw flash` "
                     "reboots into the bootloader itself" % n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 epilog="\n".join(__doc__.splitlines()[1:]),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="MicroFreak",
                    help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the plugins' checks of the instrument")
    ap.add_argument("--peek-base", type=lambda s: int(s, 0), default=None,
                    metavar="ADDR", help=argparse.SUPPRESS)
    ap.add_argument("--offline", action="store_true",
                    help="check this machine only; do not open the port")
    a = ap.parse_args(argv)

    r = Report()
    tier_offline(r)
    if not a.offline:
        tier_device(r, a.port, a)
        tier_flash(r)

    print()
    jsonout.result(ready=not r.broken, offline_only=bool(a.offline),
                   missing=r.broken, checks=r.checks)
    if r.broken:
        print("not ready -- missing: %s" % ", ".join(r.broken))
        return 1
    print("ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
