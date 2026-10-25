"""`mfreak doctor` checks for the speech plugin."""
import platform
import shutil
import time

from microfreak.firmware.peek import PEEK_OPCODE, peek_message, peek_reply

import mfreak_speech.sysex as X

# Voice 0's LPCSpeechSynthWordBank pointer. Its address septets are all below
# 0x48, and on a patched instrument that has played Speech it points into the
# RAM table.
PROBE_ADDR = X.BANK_BASE


def offline(r):
    if platform.system() == "Darwin":
        p = shutil.which("say")
        if p:
            r.ok("say", p)
        else:
            r.bad("say", "MISSING", blocks="speech say from TEXT (audio files "
                  "still work)", fix="it ships with macOS; check your PATH",
                  fatal=False)
    else:
        r.note("say", "not macOS -- speech say needs an audio file, not text")


def _probe(mo, mi, base=None, wait=2.5):
    """Does the instrument answer the memory read? -> (verdict, detail)."""
    seq = 1
    try:
        msg = peek_message(PROBE_ADDR, seq=seq, base=base)
    except SystemExit as e:
        return "skipped", "could not build the probe: %s" % e
    assert all(b < 0x48 for b in msg[6:-1]), "probe message is not benign"
    while mi.get_message():
        pass
    mo.send_message(list(msg))
    end = time.time() + wait
    while time.time() < end:
        m = mi.get_message()
        if m:
            got = peek_reply(bytes(m), seq=seq)
            if got is not None:
                return "live", "0x%08X reads 0x%08X" % (PROBE_ADDR, got)
        time.sleep(0.005)
    return "stock", None


def device(r, mo, mi, args, reply):
    from microfreak import plugin
    what, rev = plugin.running(reply)
    if what not in ("speech", "stock"):
        r.bad("speech patch", "NO -- it runs %s" % (
                  "official firmware %d.%d.%d" % tuple(reply[12:15])
                  if what == "official" else
                  "stock firmware with embedded words, which takes no uploads"
                  if what == "words" else
                  "the %s patch" % what if what else "another patched build"),
              blocks="speech say, speech banks  (they would SILENTLY do nothing)",
              fix=FIX, fatal=False)
        return
    verdict, detail = _probe(mo, mi, base=args.peek_base)
    if verdict == "live" and what == "speech":
        from mfreak_speech.sysex import PATCH_REV
        r.ok("speech patch", "LIVE -- rev %d%s, answers opcode 0x%02X; %s"
             % (rev, "" if rev >= PATCH_REV else
                " (rev %d is current; rebuild and flash to update)" % PATCH_REV,
                PEEK_OPCODE, detail))
    elif verdict == "live":
        r.note("speech patch", "a patched build older than speech18, which "
               "does not report itself; it answers the memory read (%s)" % detail)
    elif verdict == "skipped":
        r.note("speech patch", detail)
    elif what == "speech":
        r.bad("speech patch", "it reports the speech patch but does not answer "
              "the memory read", blocks="speech say, speech banks",
              fix="power-cycle it, then run doctor again", fatal=False)
    else:
        r.bad("speech patch", "NO -- this instrument is not running it",
              blocks="speech say, speech banks  (they would SILENTLY do nothing)",
              fix=FIX, fatal=False)


FIX = ["mfreak fw patch speech <the stock 5.0.0.2084 .mff> --out speech.mff",
       "mfreak fw flash speech.mff   (WRITES FLASH)",
       "speech preview needs none of this and works now"]
