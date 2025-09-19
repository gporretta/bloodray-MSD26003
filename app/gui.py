import sys
import time
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime

from config import (
    WINDOW_GEOMETRY, BG_DARK, ROTATIONS, ROTATION_DELAY_S, LIGHT_THRESHOLD
)
from app.motor import StepperMotor
from app.adc import ADCReader
from app.db import init_db, save_test_result, export_to_excel

class TestApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Tool Test System")
        self.root.geometry(WINDOW_GEOMETRY)
        self.root.configure(bg=BG_DARK)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.container = tk.Frame(self.root, bg=BG_DARK)
        self.container.pack(fill="both", expand=True)

        self.adc = ADCReader()
        self.max_light = 0

        # progress animation bookkeeping
        self._progress_after_id = None
        self._animating = False

        init_db()
        self.show_home_screen()

    # ---------- lifecycle ----------
    def on_close(self):
        self.stop_progress_animation()
        self.adc.stop()
        self.root.after(50, self.root.destroy)

    def clear_screen(self):
        self.stop_progress_animation()
        for w in self.container.winfo_children():
            w.destroy()
        self.container.unbind("<Button-1>")

    # ---------- screens ----------
    def show_home_screen(self):
        self.clear_screen()

        title = tk.Label(self.container, text="Tool Test System",
                         font=("Arial", 28, "bold"), fg="white", bg=BG_DARK)
        title.pack(pady=30)

        tool_frame = tk.Frame(self.container, bg=BG_DARK)
        tool_frame.pack(pady=15)
        tk.Label(tool_frame, text="Tool Type:", font=("Arial", 16),
                 fg="white", bg=BG_DARK).pack(side="left", padx=10)
        self.tool_var = tk.StringVar(value="Scalpel")
        ttk.Combobox(
            tool_frame, textvariable=self.tool_var,
            values=["Scalpel", "Forceps", "Retractor", "Clamp", "Scissors"],
            font=("Arial", 14), state="readonly", width=15
        ).pack(side="left", padx=10)

        name_frame = tk.Frame(self.container, bg=BG_DARK)
        name_frame.pack(pady=15)
        tk.Label(name_frame, text="Your Name:", font=("Arial", 16),
                 fg="white", bg=BG_DARK).pack(side="left", padx=10)
        self.name_var = tk.StringVar()
        tk.Entry(name_frame, textvariable=self.name_var,
                 font=("Arial", 14), width=20).pack(side="left", padx=10)

        self.start_btn = tk.Button(
            self.container, text="START TEST",
            font=("Arial", 24, "bold"),
            bg="#4CAF50", fg="white", activebackground="#45a049",
            relief="flat", command=self.start_test_thread
        )
        self.start_btn.pack(pady=20, ipadx=40, ipady=20)
        self.start_btn.config(state="normal")

        tk.Button(
            self.container, text="EXPORT TO EXCEL",
            font=("Arial", 18, "bold"),
            bg="#2196F3", fg="white", activebackground="#1976D2",
            relief="flat", command=export_to_excel
        ).pack(pady=10, ipadx=20, ipady=10)

    def show_progress_screen(self):
        self.clear_screen()
        self.progress_label = tk.Label(
            self.container, text="Test in Progress",
            font=("Arial", 36, "bold"), fg="black", bg="white"
        )
        self.progress_label.pack(expand=True, fill="both")
        self.progress_dots = 0
        self.start_progress_animation()

    def show_result_screen(self, text, color):
        self.clear_screen()
        result_label = tk.Label(
            self.container, text=text, font=("Arial", 48, "bold"),
            fg="white", bg=color
        )
        result_label.pack(expand=True, fill="both")

        current_time = datetime.now().strftime("%H:%M:%S")
        time_label = tk.Label(
            self.container, text=f"Test completed: {current_time}",
            font=("Arial", 16, "bold"), fg="white", bg=color
        )
        time_label.place(relx=0.99, rely=0.01, anchor="ne")

        result_label.bind("<Button-1>", lambda e: self.show_home_screen())

    # ---------- animation ----------
    def start_progress_animation(self):
        self._animating = True
        self._animate_tick()

    def stop_progress_animation(self):
        self._animating = False
        if self._progress_after_id is not None:
            try:
                self.root.after_cancel(self._progress_after_id)
            except Exception:
                pass
            self._progress_after_id = None

    def _animate_tick(self):
        if not self._animating:
            return
        if not hasattr(self, "progress_label") or not self.progress_label.winfo_exists():
            self.stop_progress_animation()
            return
        dots = "." * (self.progress_dots % 4)
        self.progress_label.config(text=f"Test in Progress{dots}")
        self.progress_dots += 1
        self._progress_after_id = self.root.after(500, self._animate_tick)

    # ---------- test flow ----------
    def start_test_thread(self):
        self.start_btn.config(state="disabled")
        self.show_progress_screen()
        print("[DEBUG] Starting test thread")
        t = threading.Thread(target=self.run_test, daemon=True)
        t.start()

    def run_test(self):
        adc_thread = threading.Thread(target=self.adc_loop, daemon=True)
        adc_thread.start()

        motor = StepperMotor()
        try:
            for r in range(ROTATIONS):
                print(f"[DEBUG] Starting rotation {r+1}/{ROTATIONS}")
                motor.rotate_90()
                print(f"[DEBUG] Rotation {r+1} complete. Waiting {ROTATION_DELAY_S}s before next rotation.")
                # Wait in small increments to be responsive to stop
                for _ in range(int(ROTATION_DELAY_S * 10)):
                    if not self.adc._running:
                        break
                    time.sleep(0.1)
        finally:
            motor.cleanup()
            self.adc.stop()
            adc_thread.join(timeout=1.0)

        self.max_light = self.adc.max_light
        test_failed = self.max_light > LIGHT_THRESHOLD
        result_text = "FAILED" if test_failed else "PASSED"
        color = "red" if test_failed else "green"
        print(f"[DEBUG] Test finished. Max light observed: {self.max_light}, Result: {result_text}")

        try:
            save_test_result(self.tool_var.get(), self.name_var.get(), result_text)
        except Exception as e:
            print(f"[ERROR] Failed to save result: {e}", file=sys.stderr)

        self.root.after(0, lambda: self.show_result_screen(result_text, color))

    def adc_loop(self):
        self.adc.loop(sleep_s=0.05)

