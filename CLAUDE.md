# Writing help text and docs

`--help` output comes from module docstrings, so a docstring is user-facing text.
Write it for someone who wants to run the command, not for someone reviewing it.

- Say what the command does, what to type, and what happens. Stop there.
- Do not argue for the design. No "why X rather than Y", no "that is what X is for".
- No history. Nothing about what used to be true, earlier builds, bugs that were
  fixed, or retractions. That goes in commit messages, or in
  microfreak-emu's docs/METHOD.md.
- Do not answer objections nobody raised, and do not cite tests as proof.
- Give a number only if it changes what the user does. "About a minute", not
  "11.5 emulated seconds at 3.45x".
- Say each thing once, at the level where it is needed. The top-level summary
  lists commands. Procedures and caveats go on the command they belong to.
- Option help is one line where possible: what it does, its default, and a
  warning if it is dangerous.
- Plain sentences. No em-dash asides, no ALL-CAPS or bold in mid-sentence, no
  rhetorical openers like "Because" or "So".
- Danger is the exception to brevity: anything that writes flash, overwrites a
  slot, or produces a file containing the serial number says so plainly.

AGENTS.md is for a program driving the tool. It states rules and contracts, not
the history of how they came to be.

The format docs (PRESET_FORMAT, SAMPLE_ENCODING) can be long, because
they hold evidence. The same rules apply to their prose: state findings and the
evidence for them, without arguing or narrating how they were reached.
