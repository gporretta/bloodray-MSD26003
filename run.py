#!/usr/bin/env python3
import tkinter as tk
import signal
import sys
from app.gui import TestApp


def main():
    root = tk.Tk()

    root.attributes("-fullscreen", True)
    root.overrideredirect(True)
    app = TestApp(root)

    def handle_sigint(sig, frame):
        try:
            app.on_close()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        root.mainloop()
    except Exception as e:
        app.on_close()
        sys.exit(1)


if __name__ == "__main__":
    main()
