"""mfreak-peek: a read-only diagnostic firmware, and the commands that use it."""
from microfreak.plugin import Command, Firmware, Plugin

plugin = Plugin(
    name="peek",
    help="read the instrument's memory, and dump your bootloader",
    doctor="mfreak_peek.doctor",
    firmware=Firmware(
        "mfreak_peek.build",
        help="stock firmware that can also read its own memory",
        identity="P"),
    commands={
        "dump-bootloader": Command(
            "mfreak_peek.bootloader",
            help="read your bootloader out (the file holds your serial)",
            device=True, writes="file",
            summary="Read your bootloader out over MIDI. Read-only on the "
                    "device. THE FILE CONTAINS YOUR SERIAL NUMBER: do not "
                    "publish it, or use --redact-serial. Needs the peek "
                    "build flashed first."),
    },
)
