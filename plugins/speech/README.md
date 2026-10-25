# mfreak-speech

Put your own words on the Arturia MicroFreak's Speech engine. A plugin for
[microfreak-tools](https://github.com/gbryant/microfreak-tools). Not
affiliated with Arturia.

    uv tool install microfreak-tools --with mfreak-speech

Hear words the way the instrument will say them, with no instrument needed:

    mfreak speech preview "hello there"

Putting them on the instrument needs its firmware patched first. This writes
flash:

    mfreak fw patch speech MicroFreak_Firmware_Update_5_0_0_2084.mff --out speech.mff
    mfreak fw flash speech.mff
    mfreak speech say "i am microfreak"

`mfreak speech` shows every step. Download the stock 5.0.0.2084 `.mff` from
the MicroFreak page on arturia.com. To remove the patch, flash the stock
`.mff`. Read "Before you flash" in the microfreak-tools README first.

Apache-2.0. The Speech synthesis is ported from Mutable Instruments Plaits,
MIT; see NOTICE.
