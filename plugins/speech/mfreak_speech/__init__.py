"""mfreak-speech: speech vocabulary uploads, and the firmware patch they need."""
from microfreak.plugin import Command, Firmware, Plugin

_PATCHED = "Needs firmware patched by `fw patch speech`."

GUIDE = """\
Put your own words on the MicroFreak's Speech engine.

  1. Hear them first. No instrument needed:
       mfreak speech preview "hello there"
     Give text, an audio file, or nothing to record yourself.

  2. Install the speech firmware, once. Download the stock 5.0.0.2084 .mff
     from the MicroFreak page on arturia.com. This writes flash:
       mfreak fw patch speech MicroFreak_Firmware_Update_5_0_0_2084.mff --out speech.mff
       mfreak fw flash speech.mff

  3. Put words on it. Set the oscillator type to Speech; Shape picks the
     word, Wave picks the bank:
       mfreak speech say hello goodbye "thank you"
       mfreak speech banks --bank one two --bank goodbye
       mfreak speech banks --from vocab.txt
     Switching the instrument off brings the factory words back.

  4. Or make them permanent, in a firmware file to flash:
       mfreak speech embed speech.mff --from vocab.txt --out speech-words.mff
       mfreak fw flash speech-words.mff

`mfreak speech banks --help` describes a vocabulary file. To remove the
firmware, flash the stock .mff.

Advanced: speech analyze, speech upload. Each has --help.
"""

plugin = Plugin(
    name="speech",
    help="put your own words on the Speech engine",
    firmware=Firmware(
        "mfreak_speech.firmware",
        help="stock firmware that also takes speech vocabulary uploads",
        identity="S"),
    doctor="mfreak_speech.doctor",
    guide=GUIDE,
    commands={
        "preview": Command(
            "mfreak_speech.preview",
            help="hear words as Speech, no instrument needed",
            writes="file",
            summary="Speak text, load audio or record, and play it back as "
                    "the Speech engine would say it. No instrument or "
                    "firmware needed."),
        "analyze": Command(
            "mfreak_speech.analyze",
            help="convert speech audio to LPC frames",
            writes="file", advanced=True,
            summary="Convert speech audio to LPC frames and report what the "
                    "engine will keep and drop. Offline."),
        "say": Command(
            "mfreak_speech.cli", ["say"],
            help="put words on it (needs patched firmware)",
            device=True, writes="device",
            summary="Put words on the instrument, one per Shape position. "
                    + _PATCHED),
        "banks": Command(
            "mfreak_speech.cli", ["banks"],
            help="one word bank per Wave knob position",
            device=True, writes="device",
            summary="Upload one word bank per Wave knob position. " + _PATCHED),
        "embed": Command(
            "mfreak_speech.embed",
            help="put your words in a firmware file",
            image=True, writes="file",
            summary="Wipe the word banks in a firmware file and write your "
                    "own. Writes a file to flash with `fw flash`."),
        "upload": Command(
            "mfreak_speech.sysex",
            help="the low-level SysEx uploader",
            device=True, writes="device", advanced=True,
            summary="The low-level SysEx uploader behind `say` and `banks`. "
                    + _PATCHED),
    },
)
