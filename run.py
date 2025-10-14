#!/usr/bin/env python3
import tkinter as tk
import signal
import sys
from app.gui import TestApp


def main():
    root = tk.Tk()

    root.attributes("-fullscreen", True)
    root.config(cursor="none")
    #root.overrideredirect(True)
    app = TestApp(root)

    def _graceful(*_):
        app.on_close()
    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    root.mainloop()

if __name__ == "__main__":
    main()
