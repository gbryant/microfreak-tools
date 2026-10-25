"""`mfreak doctor` checks for the peek plugin."""
from microfreak import plugin


def device(r, mo, mi, args, reply):
    if plugin.running(reply)[0] == "peek":
        r.ok("peek build", "running; `mfreak peek dump-bootloader` will work")
    else:
        r.note("peek build", "not running; only `peek dump-bootloader` needs it")
