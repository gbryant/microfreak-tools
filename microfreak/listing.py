"""Printing a list as it is read off the instrument, shared by the list commands.

Rows print as they arrive. Consecutive rows with the same run key, such as
300-odd empty presets, print as one line when the run ends. On a terminal, a
status line on stderr shows which item is being read while nothing else is
printing.
"""
import sys


class Listing:
    def __init__(self, header, noun, end, width=6, collapse=True, span=None):
        """`noun` names one item ("preset"); `end` is the last panel number.
        A run prints as `first-last` after a number `width` wide, or, with
        `span`, right-aligned in a column that wide."""
        self.noun, self.end, self.width, self.collapse = noun, end, width, collapse
        self.span = span
        self.run = None          # [first, last, key, label, line]
        self.status = sys.stderr.isatty()
        print(header, flush=True)

    def _clear(self):
        if self.status:
            sys.stderr.write("\r\033[K")

    def _print(self, text):
        self._clear()
        print(text, flush=True)

    def _end_run(self):
        if not self.run:
            return
        first, last, _, label, line = self.run
        self.run = None
        if first == last:
            self._print(line)
        else:
            count = "   (%d %ss)" % (last - first + 1, self.noun)
            if self.span:
                self._print("%*s  %s%s" % (self.span, "%d-%d" % (first, last),
                                           label, count))
            else:
                self._print("%*d-%-5d %s%s" % (self.width, first, last, label, count))

    def add(self, number, line, key=None, label=None):
        """One item. `line` is its row printed alone. Items with the same
        non-None `key` in a row print as one run, as `label`."""
        if (self.collapse and key is not None and self.run
                and self.run[2] == key and self.run[1] == number - 1):
            self.run[1] = number
        else:
            self._end_run()
            if self.collapse and key is not None:
                self.run = [number, number, key, label, line]
            else:
                self._print(line)
        self.reading(number)

    def reading(self, number):
        """Show progress for an item that printed nothing, such as a hidden one."""
        if self.status:
            sys.stderr.write("\r\033[K  reading %s %d of %d"
                             % (self.noun, number, self.end))
            sys.stderr.flush()

    def finish(self):
        self._end_run()
        self._clear()
