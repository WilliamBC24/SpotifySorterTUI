#!/usr/bin/env python3
"""Simple TUI key input demo for arrow keys and Enter."""

from __future__ import annotations

import curses


KEY_LABELS = {
    curses.KEY_UP: "UP",
    curses.KEY_DOWN: "DOWN",
    curses.KEY_LEFT: "LEFT",
    curses.KEY_RIGHT: "RIGHT",
    curses.KEY_ENTER: "ENTER",
    10: "ENTER",
    13: "ENTER",
}


def run(stdscr: curses.window) -> None:
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.keypad(True)

    history: list[str] = []

    while True:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()

        title = "Spotify Sorter TUI - Input Demo"
        help_text = "Press arrow keys or Enter. Press q to quit."
        width = max(1, cols - 1)

        stdscr.addnstr(0, 0, title, width)
        stdscr.addnstr(1, 0, help_text, width)
        stdscr.hline(2, 0, "-", width)
        stdscr.addnstr(3, 0, "Captured input:", width)

        available_lines = max(1, rows - 5)
        visible_history = history[-available_lines:]
        start_number = len(history) - len(visible_history) + 1
        for row_offset, item in enumerate(visible_history):
            event_number = start_number + row_offset
            stdscr.addnstr(4 + row_offset, 0, f"{event_number}. {item}", width)

        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break

        label = KEY_LABELS.get(key, f"KEYCODE {key}")
        history.append(label)


def main() -> None:
    curses.wrapper(run)


if __name__ == "__main__":
    main()
