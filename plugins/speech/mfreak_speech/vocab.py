"""Vocabulary files: which words go on which Wave position.

    # vocab.txt
    [1]
    hello
    good morning
    recordings/my-name.wav

    [3]
    goodbye

`[1]` to `[5]` are Wave positions. Each line under one is a Shape position:
text to speak, or an audio file, relative to the vocabulary file. A
position with no heading repeats the nearest bank below it, or the first
bank if there is none below. A file with no headings is bank 1. Blank lines
and lines starting with # are skipped.
"""
import os
import re
import sys

NUM_BANKS = 5
MAX_WORDS = 31


def parse(path):
    """-> [[item, ...] per Wave position], five lists, empty where no words.
    An item is ("audio", path) or ("text", str)."""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except UnicodeDecodeError:
        sys.exit("%s: not a text file" % path)
    here = os.path.dirname(os.path.abspath(path))
    banks = [[] for _ in range(NUM_BANKS)]
    headed = any(re.fullmatch(r"\[.*\]", l.strip()) for l in lines)
    seen, current = set(), None if headed else 0
    for n, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.*)\]", line)
        if m:
            if not re.fullmatch(r"[1-5]", m.group(1).strip()):
                sys.exit("%s:%d: %s is not a Wave position; use [1] to [%d]"
                         % (path, n, line, NUM_BANKS))
            current = int(m.group(1).strip()) - 1
            if current in seen:
                sys.exit("%s:%d: [%d] appears twice" % (path, n, current + 1))
            seen.add(current)
            continue
        if current is None:
            sys.exit("%s:%d: words before the first heading" % (path, n))
        audio = os.path.join(here, line)
        banks[current].append(("audio", audio) if os.path.isfile(audio)
                              else ("text", line))
        if len(banks[current]) > MAX_WORDS:
            sys.exit("%s:%d: bank %d has more than %d words"
                     % (path, n, current + 1, MAX_WORDS))
    if not any(banks):
        sys.exit("%s: no words in it" % path)
    return banks


def from_flags(groups):
    """-> the same shape from `--bank` groups, in order from Wave position 1."""
    if len(groups) > NUM_BANKS:
        sys.exit("%d banks given; the Wave knob has %d positions"
                 % (len(groups), NUM_BANKS))
    banks = [[] for _ in range(NUM_BANKS)]
    for i, group in enumerate(groups):
        banks[i] = [("audio", g) if os.path.isfile(g) else ("text", g)
                    for g in group]
    return banks


def sources(banks):
    """-> for each Wave position, the index of the bank it plays: its own if
    it has words, else the nearest one below with words, else the first."""
    filled = [i for i, b in enumerate(banks) if b]
    return [i if banks[i] else max([j for j in filled if j < i] or filled[:1])
            for i in range(len(banks))]


def repeats(banks):
    """-> "Wave position 2 repeats bank 1. Wave positions 4 and 5 repeat
    bank 3.", or None when every position has words."""
    groups = {}
    for i, s in enumerate(sources(banks)):
        if s != i:
            groups.setdefault(s, []).append(i + 1)
    return " ".join(
        "Wave position%s %s %s bank %d." % ("s" if len(p) > 1 else "", _join(p),
                                            "repeat" if len(p) > 1 else "repeats", s + 1)
        for s, p in sorted(groups.items(), key=lambda g: g[1][0])) or None


def _join(nums):
    nums = [str(n) for n in nums]
    return nums[0] if len(nums) == 1 else ", ".join(nums[:-1]) + " and " + nums[-1]
