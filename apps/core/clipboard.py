"""Clipboard helper with a safe no-op fallback.

``pyperclip`` is a desktop convenience and requires platform clipboard
backends that are absent on a headless server.  Wrapping it here keeps the
service layer importable in a web process without installing those backends.
"""


def copy(text):
    """Copy ``text`` to the clipboard if a backend is available."""
    try:
        import pyperclip

        pyperclip.copy(text)
    except Exception:
        # Headless / no clipboard backend: silently ignore.
        pass
