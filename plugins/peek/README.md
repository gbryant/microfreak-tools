# mfreak-peek

A read-only diagnostic firmware patch for the Arturia MicroFreak, and a
bootloader dump. A plugin for
[microfreak-tools](https://github.com/gbryant/microfreak-tools). Not
affiliated with Arturia.

    uv tool install microfreak-tools --with mfreak-peek

The patch is stock firmware that can also read its own memory over SysEx.
Installing it writes flash:

    mfreak fw patch peek MicroFreak_Firmware_Update_5_0_0_2084.mff --out peek.mff
    mfreak fw flash peek.mff
    mfreak peek dump-bootloader --out bootloader.bin

The bootloader dump contains your instrument's serial number. Keep it
private. To remove the patch, flash the stock `.mff`. Read "Before you
flash" in the microfreak-tools README first.

Apache-2.0.
