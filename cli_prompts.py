"""Terminal input helpers (Windows paste quirks)."""

import sys

PASTE_HINT = (
    "Tip: on Windows, right-click or Shift+Insert to paste "
    "(Ctrl+V often types ^V instead)."
)


def prompt_input(message: str, *, show_paste_hint: bool = True) -> str:
    """
    Read a line from the terminal, with Windows paste guidance and ^V recovery.
    """
    if show_paste_hint and sys.platform == "win32":
        print(PASTE_HINT)

    value = input(message).strip()
    # Ctrl+V in some Windows terminals sends ASCII 22 (SYN) literally
    if value in ("\x16", "^V") or value == "\x16":
        print(
            "\n  Paste did not work. Right-click the terminal and choose Paste,"
        )
        print("  press Shift+Insert, or pass the value via a command-line flag.\n")
        return prompt_input(message, show_paste_hint=False)

    return value.replace("\x16", "")
