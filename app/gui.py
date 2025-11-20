#!/usr/bin/env python3
# gui.py

import sys
import os
import sqlite3
import time
import threading
import tkinter as tk
from tkinter import messagebox
from datetime import datetime
from PIL import Image, ImageTk
import cv2
import numpy as np
import RPi.GPIO as GPIO

from app.config import (
    WINDOW_GEOMETRY, BG_DARK, ROTATIONS, ROTATION_DELAY_S, LIGHT_THRESHOLD  # LIGHT_THRESHOLD unused now
)
from app.motor import StepperMotor
from app.camera import CameraReader
from app.db import init_db, save_run  # DB split: external module

# --- Robust-threshold tunables ---
GUARD_ABS = 0.10      # absolute guard band (brightness units)
GUARD_SIGMA = 3.0     # multiplier on baseline std dev


class TestApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Automated Tool Test System")
        self.root.geometry(WINDOW_GEOMETRY)
        self.root.configure(bg=BG_DARK)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", lambda e: self.on_close())

        self.container = tk.Frame(self.root, bg=BG_DARK)
        self.container.pack(fill="both", expand=True)

        self.camera = CameraReader()
        self.max_light = 0.0
        self.dynamic_threshold = None       # baseline p95
        self.effective_threshold = None     # baseline p95 + guard band

        # progress animation bookkeeping
        self._progress_after_id = None
        self._animating = False

        # camera preview bookkeeping
        self._camera_preview_id = None
        self.preview_w = None
        self.preview_h = None

        # heatmap accumulation (per-run)
        self._heatmap_max = None           # np.ndarray (float32), max-projection of grayscale frames
        self.last_heatmap_path = None      # str path to saved heatmap PNG
        self.last_pct_above_thr = None     # float percentage of pixels over effective threshold

        # legacy preview reader placeholders (unused now)
        self._mist_preview_stop = None
        self._mist_preview_thread = None

        # metrics/timing (kept in-memory, persisted to DB at run end)
        self._lock = threading.Lock()
        self.metrics = self._new_metrics()

        # GPIO for misting relay (GPIO17, BCM)
        self.GPIO = None
        self.MIST_PIN = 17
        self._init_gpio()

        # Live-preview control
        self._live_preview_thread = None
        self._live_preview_stop = None

        # Misting jog control
        self._mist_jog_thread = None
        self._mist_jog_stop = None

        # Initialize DB schema
        init_db()

        self.show_home_screen()

    def _init_gpio(self):
        """
        Initialize Raspberry Pi GPIO (BCM mode) and set MIST_PIN as output LOW.
        Fails gracefully on non-Pi/dev environments.
        """
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.MIST_PIN, GPIO.OUT, initial=GPIO.LOW)
            self.GPIO = GPIO
        except Exception:
            self.GPIO = None  # running off Pi or GPIO not available; silently ignore

    def _gpio_cleanup(self):
        try:
            if self.GPIO is not None:
                self.GPIO.cleanup()
        except Exception:
            pass

    def _mist_on(self):
        try:
            if self.GPIO is not None:
                self.GPIO.output(self.MIST_PIN, self.GPIO.HIGH)
        except Exception:
            pass

    def _mist_off(self):
        try:
            if self.GPIO is not None:
                self.GPIO.output(self.MIST_PIN, self.GPIO.LOW)
        except Exception:
            pass

    def _mist_worker(self, seconds: float):
        """
        Background worker: drive the mist relay HIGH for `seconds` then turn it off.
        Runs on its own thread so it can overlap with rotation and camera acquisition.
        """
        self._init_gpio()
        if seconds <= 0:
            return
        start = time.perf_counter()
        #self._mist_on()
        GPIO.output(self.MIST_PIN, self.GPIO.HIGH)  # 3.3 V
        try:
            time.sleep(seconds)
        finally:
            GPIO.output(self.MIST_PIN, self.GPIO.LOW)  # 0V
            #self._mist_off()
            _ = time.perf_counter() - start

    def _rotation_worker(self, revolutions: float = 1.0):
        """
        Background worker: rotate the stepper `revolutions` * 360 degrees total.
        Uses 4 x 90-degree segments per full revolution.
        """
        if revolutions <= 0:
            return
        try:
            motor = StepperMotor()
        except Exception:
            # If GPIO / motor isn't available (e.g., dev laptop), just skip rotation.
            return

        try:
            segments = int(max(1, round(4.0 * revolutions)))  # 4 * 90° per revolution
            start = time.perf_counter()
            for _ in range(segments):
                motor.rotate_90()
            elapsed = time.perf_counter() - start
            with self._lock:
                self.metrics["rotation_time_accum"] += float(elapsed)
        finally:
            try:
                motor.cleanup()
            except Exception:
                pass

    def _new_metrics(self):
        return {
            # timers (perf_counter seconds)
            "total_start": None,
            "total_end": None,
            "baseline_start": None,
            "baseline_end": None,
            "analysis_start": None,
            "analysis_end": None,
            "rotation_time_accum": 0.0,

            # frame counts / errors
            "frames_total": 0,
            "frames_baseline": 0,
            "frames_analysis": 0,
            "read_errors": 0,

            # brightness & detection
            "baseline_samples": [],
            "baseline_mean": None,
            "baseline_std": None,
            "baseline_p95": None,
            "guard_band": None,
            "effective_threshold": None,
            "first_exceed_time": None,   # seconds since analysis_start
            "max_brightness": 0.0,

            # heatmap & contamination
            "heatmap_png_path": None,
            "pct_frame_above_threshold": None,
        }

    # ---------- lifecycle ----------
    def on_close(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True

        # Stop any live preview and mist jog
        self._stop_live_preview_thread()
        self._stop_mist_jog_thread()

        self.stop_progress_animation()
        self.stop_camera_preview()
        try:
            self.camera.stop()
            self.camera.close_camera()
        except Exception:
            pass
        self._gpio_cleanup()
        self.root.after(50, self.root.destroy)

    def clear_screen(self):
        # Also stop live preview / mist jog when we clear screens, to avoid
        # orphaned background workers tied to a dead GUI.
        self._stop_live_preview_thread()
        self._stop_mist_jog_thread()

        self.stop_progress_animation()
        self.stop_camera_preview()
        for w in self.container.winfo_children():
            w.destroy()
        self.container.unbind("<Button-1>")
        self.root.after(0, self.root.focus_force)

    # ---------- screens ----------
    def show_home_screen(self):
        self.clear_screen()

        title = tk.Label(
            self.container, text="Bloodray Automated Tool Test System",
            font=("Arial", 26, "bold"), fg="white", bg=BG_DARK
        )
        title.pack(pady=(18, 8))

        self.start_btn = tk.Button(
            self.container, text="START TEST",
            font=("Arial", 22, "bold"),
            bg="#4CAF50", fg="white", activebackground="#45a049",
            relief="flat", command=self.start_test_thread
        )
        self.start_btn.pack(pady=(10, 12), ipadx=36, ipady=16)
        self.start_btn.config(state="normal")

        # LIVE PREVIEW button (camera only, no mist/motor/DB)
        self.live_btn = tk.Button(
            self.container, text="LIVE PREVIEW",
            font=("Arial", 20, "bold"),
            bg="#9C27B0", fg="white", activebackground="#7B1FA2",
            relief="flat", command=self.start_live_preview
        )
        self.live_btn.pack(pady=(6, 6), ipadx=26, ipady=12)

        # MISTING JOG button (run mist pump until user stops)
        self.mist_btn = tk.Button(
            self.container, text="MISTING JOG",
            font=("Arial", 20, "bold"),
            bg="#FF9800", fg="white", activebackground="#F57C00",
            relief="flat", command=self.start_mist_jog
        )
        self.mist_btn.pack(pady=(6, 10), ipadx=30, ipady=12)

        # EXPORT TO EXCEL button
        export_btn = tk.Button(
            self.container, text="EXPORT TO EXCEL",
            font=("Arial", 18, "bold"),
            bg="#2196F3", fg="white", activebackground="#1976D2",
            relief="flat", command=self.export_to_excel
        )
        export_btn.pack(pady=(8, 12), ipadx=18, ipady=10)

    def show_progress_screen(self):
        """Live camera preview with animated LOADING... in top-right."""
        self.clear_screen()

        self.preview_w = 780
        self.preview_h = 480
        self.camera_canvas = tk.Canvas(
            self.container, width=self.preview_w, height=self.preview_h,
            bg="black", highlightthickness=0
        )
        self.camera_canvas.pack(pady=(8, 8))

        # Animated LOADING... badge (top-right overlay)
        self.loading_label = tk.Label(
            self.container, text="LOADING", font=("Arial", 14, "bold"),
            fg="white", bg=BG_DARK
        )
        self.loading_label.place(relx=0.99, rely=0.02, anchor="ne")

        self.progress_dots = 0
        self.start_progress_animation()
        self.start_camera_preview()

    def show_live_preview_screen(self):
        """
        Screen for standalone live preview:
        - camera preview canvas
        - "STOP LIVE PREVIEW" button to stop and return home
        """
        self.clear_screen()

        self.preview_w = 600
        self.preview_h = 400
        self.camera_canvas = tk.Canvas(
            self.container, width=self.preview_w, height=self.preview_h,
            bg="black", highlightthickness=0
        )
        self.camera_canvas.pack(pady=(18, 10))

        stop_btn = tk.Button(
            self.container, text="STOP LIVE PREVIEW",
            font=("Arial", 20, "bold"),
            bg="#F44336", fg="white", activebackground="#D32F2F",
            relief="flat", command=self.end_live_preview
        )
        stop_btn.pack(pady=(6, 18), ipadx=24, ipady=12)

        # Start only the GUI update loop here (worker thread handles camera I/O)
        self._update_camera_preview()

    def show_mist_jog_screen(self):
        """
        Screen for mist jog:
        - text label
        - "STOP MISTING" button to stop and return home
        """
        self.clear_screen()

        label = tk.Label(
            self.container,
            text="Misting Jog Active\nPump is pulsing until you stop it.",
            font=("Arial", 24, "bold"),
            fg="white",
            bg=BG_DARK,
            justify="center"
        )
        label.pack(expand=True, fill="both", pady=(40, 20), padx=20)

        stop_btn = tk.Button(
            self.container, text="STOP MISTING",
            font=("Arial", 20, "bold"),
            bg="#F44336", fg="white", activebackground="#D32F2F",
            relief="flat", command=self.stop_mist_jog
        )
        stop_btn.pack(pady=(0, 40), ipadx=28, ipady=14)

    def show_seal_warning_screen(self):
        """Yellow screen advising the user to seal the box; click to return home."""
        self.clear_screen()
        color = "#FFCC00"  # yellow
        result_label = tk.Label(
            self.container,
            text="Ensure Box is Sealed",
            font=("Arial", 36, "bold"),
            fg="black",
            bg=color
        )
        result_label.pack(expand=True, fill="both")
        # tiny hint + timestamp
        ts = datetime.now().strftime("%H:%M:%S")
        meta_label = tk.Label(
            self.container, text=f"Baseline too bright (≥ 1.0) {ts}",
            font=("Arial", 14, "bold"), fg="black", bg=color
        )
        meta_label.place(relx=0.99, rely=0.02, anchor="ne")
        result_label.bind("<Button-1>", lambda e: self.show_home_screen())

    def show_result_screen(self, text, color):
        """
        First post-result screen (PASS/FAIL).
        Tap once -> heatmap view screen; tap again from heatmap -> home.
        """
        self.clear_screen()
        result_label = tk.Label(
            self.container, text=text, font=("Arial", 44, "bold"),
            fg="white", bg=color
        )
        result_label.pack(expand=True, fill="both")

        # small telemetry line (on-screen only)
        eff_thr = self.effective_threshold if self.effective_threshold is not None else self.dynamic_threshold
        guard = (self.effective_threshold - self.dynamic_threshold) if (self.effective_threshold is not None and self.dynamic_threshold is not None) else None
        meta_parts = []
        meta_parts.append(f"Baseline p95: {self.dynamic_threshold:.2f}" if self.dynamic_threshold is not None else "Baseline p95: N/A")
        meta_parts.append(f"Eff Thr: {eff_thr:.2f}" if eff_thr is not None else "Eff Thr: N/A")
        meta_parts.append(f"Max: {self.max_light:.2f}")
        meta = "  |  ".join(meta_parts)

        meta_label = tk.Label(
            self.container, text=meta,
            font=("Arial", 14, "bold"), fg="white", bg=color
        )
        meta_label.place(relx=0.01, rely=0.02, anchor="nw")

        current_time = datetime.now().strftime("%H:%M:%S")
        time_label = tk.Label(
            self.container, text=f"Completed: {current_time}",
            font=("Arial", 14, "bold"), fg="white", bg=color
        )
        time_label.place(relx=0.99, rely=0.02, anchor="ne")

        # Tap -> heatmap screen if we have one; otherwise go home
        if self.last_heatmap_path and os.path.exists(self.last_heatmap_path):
            result_label.bind("<Button-1>", lambda e: self.show_heatmap_screen())
        else:
            result_label.bind("<Button-1>", lambda e: self.show_home_screen())

    def show_heatmap_screen(self):
        """Second post-result screen: show saved heatmap PNG; tap -> home."""
        self.clear_screen()

        bg = "#111111"
        self.container.configure(bg=bg)

        # Title / metrics line
        header = tk.Frame(self.container, bg=bg)
        header.pack(fill="x", pady=(8, 4))
        title_lbl = tk.Label(
            header, text="Contamination Heatmap",
            font=("Arial", 24, "bold"), fg="white", bg=bg
        )
        title_lbl.pack(side="left", padx=(12, 8))

        if self.last_pct_above_thr is not None and self.effective_threshold is not None:
            sub = (f"")
        else:
            sub = "Heatmap unavailable"
        sub_lbl = tk.Label(header, text=sub, font=("Arial", 14, "bold"), fg="#CCCCCC", bg=bg)
        sub_lbl.pack(side="left")

        # Canvas for image
        canvas = tk.Canvas(self.container, bg="black", highlightthickness=0, width=780, height=440, cursor="hand2")
        canvas.pack(pady=(4, 12))

        # Load & fit image
        if self.last_heatmap_path and os.path.exists(self.last_heatmap_path):
            try:
                img_bgr = cv2.imread(self.last_heatmap_path, cv2.IMREAD_COLOR)
                disp = cv2.resize(img_bgr, (780, 440), interpolation=cv2.INTER_AREA)
                disp_rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
                pil = Image.fromarray(disp_rgb)
                photo = ImageTk.PhotoImage(pil)
                canvas.create_image(0, 0, anchor=tk.NW, image=photo)
                canvas.image = photo
            except Exception:
                pass

        canvas.bind("<Button-1>", lambda e: self.show_home_screen())
        self.container.bind("<Button-1>", lambda e: self.show_home_screen())

        hint = tk.Label(
            self.container, text="Tap anywhere to return to Home",
            font=("Arial", 16, "bold"), fg="#DDDDDD", bg=bg
        )
        hint.pack(pady=(6, 10))

    # ---------- camera preview ----------
    def start_camera_preview(self):
        try:
            self.camera.open_camera()
        except Exception:
            pass
        self._update_camera_preview()

    def stop_camera_preview(self):
        if self._camera_preview_id is not None:
            try:
                self.root.after_cancel(self._camera_preview_id)
            except Exception:
                pass
            self._camera_preview_id = None

    def _update_camera_preview(self):
        """
        GUI preview only uses the latest frame captured by the camera thread.
        It does NOT call read_frame() itself to avoid multiple threads hitting
        the VideoCapture at the same time.
        """
        try:
            if not hasattr(self, 'camera_canvas') or not self.camera_canvas.winfo_exists():
                self.stop_camera_preview()
                return

            frame = getattr(self.camera, "last_frame", None)
            if frame is None:
                self._camera_preview_id = self.root.after(33, self._update_camera_preview)
                return

            disp = cv2.resize(frame, (self.preview_w, self.preview_h), interpolation=cv2.INTER_AREA)
            frame_rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            photo = ImageTk.PhotoImage(image=img)

            self.camera_canvas.create_image(0, 0, anchor=tk.NW, image=photo)
            self.camera_canvas.image = photo

            self._camera_preview_id = self.root.after(33, self._update_camera_preview)  # ~30 FPS
        except Exception:
            self.stop_camera_preview()

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
        if hasattr(self, "loading_label") and self.loading_label.winfo_exists():
            dots = "." * (self.progress_dots % 4)
            self.loading_label.config(text=f"LOADING{dots}")
            self.progress_dots += 1
        self._progress_after_id = self.root.after(500, self._animate_tick)

    # ---------- live preview control ----------
    def _live_preview_worker(self, stop_event: threading.Event):
        """
        Background worker for standalone live preview.
        Continuously reads frames and updates camera.last_frame until stop_event is set.
        No metrics, no heatmap, no DB writes, no mist, no rotation.
        """
        try:
            self.camera.open_camera()
        except Exception:
            return

        try:
            while not stop_event.is_set():
                try:
                    frame = self.camera.read_frame()
                    self.camera.last_frame = frame
                except Exception:
                    time.sleep(0.1)
                    continue
                time.sleep(0.05)
        finally:
            try:
                self.camera.close_camera()
            except Exception:
                pass

    def _stop_live_preview_thread(self):
        """
        Internal: stop live-preview worker thread (if running) and reset flags.
        Does not change screens by itself.
        """
        if self._live_preview_stop is not None:
            self._live_preview_stop.set()
        if self._live_preview_thread is not None and self._live_preview_thread.is_alive():
            self._live_preview_thread.join(timeout=1.0)
        self._live_preview_thread = None
        self._live_preview_stop = None

    def start_live_preview(self):
        """
        Entry from HOME: show live-preview screen and start background
        frame reader. This does NOT run mist or rotation or DB/test logic.
        """
        # Ensure any other modes are off
        self._stop_mist_jog_thread()

        self.show_live_preview_screen()

        stop_event = threading.Event()
        self._live_preview_stop = stop_event
        t = threading.Thread(target=self._live_preview_worker, args=(stop_event,), daemon=True)
        self._live_preview_thread = t
        t.start()

    def end_live_preview(self):
        """
        User-initiated end of live preview: stop worker, stop preview,
        and return to home.
        """
        self._stop_live_preview_thread()
        self.stop_camera_preview()
        self.show_home_screen()

    # ---------- mist jog control ----------
    def _mist_jog_worker(self, stop_event: threading.Event):
        """
        Background worker: pulses the mist pump ON/OFF until stop_event is set.
        Pulsed behavior: e.g., 0.2 s ON, 0.8 s OFF.
        """
        self._init_gpio()
        
        ON_SEC = 1.0
        OFF_SEC = 0.5

        while not stop_event.is_set():
            #self._mist_on()
            GPIO.output(self.MIST_PIN, self.GPIO.HIGH)  # 5V
            time.sleep(ON_SEC)
            #self._mist_off()
            GPIO.output(self.MIST_PIN, self.GPIO.LOW)  # 0V
            # Check again in case stop was requested during ON time
            if stop_event.is_set():
                break
            time.sleep(OFF_SEC)

        # Ensure pump is OFF when stopping
        GPIO.output(self.MIST_PIN, self.GPIO.LOW)  # 0V
        #self._mist_off()

    def _stop_mist_jog_thread(self):
        """Internal: stop mist-jog worker if running, no UI changes."""
        if self._mist_jog_stop is not None:
            self._mist_jog_stop.set()
        if self._mist_jog_thread is not None and self._mist_jog_thread.is_alive():
            self._mist_jog_thread.join(timeout=1.0)
        self._mist_jog_thread = None
        self._mist_jog_stop = None

    def start_mist_jog(self):
        """
        Entry from HOME: show mist-jog screen and start background mist worker.
        """
        # Ensure live preview is off so modes don't overlap
        self._stop_live_preview_thread()

        self.show_mist_jog_screen()

        stop_event = threading.Event()
        self._mist_jog_stop = stop_event
        t = threading.Thread(target=self._mist_jog_worker, args=(stop_event,), daemon=True)
        self._mist_jog_thread = t
        t.start()

    def stop_mist_jog(self):
        """
        User-initiated stop: end mist jog and return home.
        """
        self._stop_mist_jog_thread()
        self.show_home_screen()

    # ---------- export ----------
    def export_to_excel(self):
        """
        Export the SQLite DB to an Excel file. Prefers 'tooltest.db'.
        If openpyxl is unavailable, falls back to CSV.
        """
        candidate_paths = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "tooltest.db")),
        ]

        db_path = None
        for p in candidate_paths:
            if os.path.exists(p):
                db_path = p
                break

        if db_path is None:
            messagebox.showerror("Export Failed", "Could not find SQLite DB (looked for tooltest.db, app/data/bloodray.db, bloodray.db).")
            return

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        xlsx_path = os.path.abspath(f"tooltest_export_{timestamp}.xlsx")
        csv_path  = os.path.abspath(f"tooltest_export_{timestamp}.csv")

        try:
            # Try Excel first (requires openpyxl)
            try:
                from openpyxl import Workbook
            except ImportError:
                Workbook = None

            with sqlite3.connect(db_path) as con:
                cur = con.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
                tables = [r[0] for r in cur.fetchall()]
                if not tables:
                    messagebox.showwarning("Export", "Database has no tables to export.")
                    return

                if Workbook is not None:
                    wb = Workbook()
                    # Remove default sheet to avoid empty "Sheet"
                    default_ws = wb.active
                    wb.remove(default_ws)

                    for table in tables:
                        cur.execute(f"SELECT * FROM {table};")
                        rows = cur.fetchall()
                        headers = [d[0] for d in cur.description]

                        safe_name = table[:31].replace(":", "_").replace("/", "_").replace("\\", "_").replace("*", "_").replace("?", "_").replace("[", "(").replace("]", ")")
                        ws = wb.create_sheet(title=safe_name)
                        ws.append(headers)
                        for r in rows:
                            ws.append(list(r))

                    wb.save(xlsx_path)
                    messagebox.showinfo("Export Complete", f"Exported to Excel:\n{xlsx_path}")
                else:
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='test_runs';")
                    has_test_runs = cur.fetchone() is not None
                    if not has_test_runs:
                        messagebox.showerror("Export Failed", "openpyxl not installed and no 'test_runs' table for CSV fallback.")
                        return

                    cur.execute("SELECT * FROM test_runs;")
                    rows = cur.fetchall()
                    headers = [d[0] for d in cur.description]

                    import csv
                    with open(csv_path, "w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow(headers)
                        writer.writerows(rows)
                    messagebox.showinfo("Export Complete", f"openpyxl not installed.\nExported CSV instead:\n{csv_path}")

        except Exception as e:
            messagebox.showerror("Export Failed", f"Error during export:\n{e}")

    # ---------- test flow ----------
    def start_test_thread(self):
        self.start_btn.config(state="disabled")
        self.show_progress_screen()
        # reset metrics per run (internal only; persisted at end)
        with self._lock:
            self.metrics = self._new_metrics()
            self.metrics["total_start"] = time.perf_counter()
        # reset heatmap accumulators
        self._heatmap_max = None
        self.last_heatmap_path = None
        self.last_pct_above_thr = None

        t = threading.Thread(target=self.run_test, daemon=True)
        t.start()

    def run_test(self):
        """
        1) Determine baseline threshold = p95 of mean-brightness over a short window
           with the box closed (no rotation yet).
        2) If baseline p95 >= 1.0, abort the run and show yellow warning (but still save).
        3) Post-baseline: start three concurrent workers:
             - camera acquisition (camera_loop thread),
             - misting relay (mist worker thread),
             - stepper rotation (rotation worker thread).
           These run simultaneously so the tool is rotating and being misted while
           frames are captured for analysis.
        4) After mist and rotation complete, keep the camera running for an additional
           dwell window (configured from ROTATIONS and ROTATION_DELAY_S).
        5) Compute contamination % of frame above effective threshold; generate heatmap.
        6) Persist run and show result screen.
        """
        timestamp_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        # --- (1) Baseline sampling (no rotation) ---
        BASELINE_SECONDS = 2.0
        SAMPLE_PERIOD_S = 0.05  # 20 Hz
        num_samples = max(5, int(BASELINE_SECONDS / SAMPLE_PERIOD_S))
        samples = []

        with self._lock:
            self.metrics["baseline_start"] = time.perf_counter()

        try:
            self.camera.open_camera()
        except Exception:
            pass

        for _ in range(num_samples):
            try:
                frame = self.camera.read_frame()
                self.camera.last_frame = frame
                lv = self.camera.measure_light_in_roi(frame)  # full-frame mean
                samples.append(lv)
                with self._lock:
                    self.metrics["frames_total"] += 1
                    self.metrics["frames_baseline"] += 1
            except Exception:
                with self._lock:
                    self.metrics["read_errors"] += 1
            time.sleep(SAMPLE_PERIOD_S)

        with self._lock:
            self.metrics["baseline_end"] = time.perf_counter()

        if not samples:
            self.dynamic_threshold = 255.0
            self.effective_threshold = 255.0
            with self._lock:
                self.metrics["baseline_samples"] = []
                self.metrics["baseline_mean"] = None
                self.metrics["baseline_std"] = None
                self.metrics["baseline_p95"] = None
                self.metrics["guard_band"] = None
                self.metrics["effective_threshold"] = 255.0
        else:
            self.dynamic_threshold = float(np.percentile(samples, 95))
            b_mean = float(np.mean(samples))
            b_std = float(np.std(samples, ddof=1)) if len(samples) > 1 else 0.0
            guard = max(GUARD_ABS, GUARD_SIGMA * b_std)
            self.effective_threshold = float(self.dynamic_threshold + guard)
            with self._lock:
                self.metrics["baseline_samples"] = samples
                self.metrics["baseline_mean"] = b_mean
                self.metrics["baseline_std"] = b_std
                self.metrics["baseline_p95"] = self.dynamic_threshold
                self.metrics["guard_band"] = float(guard)
                self.metrics["effective_threshold"] = float(self.effective_threshold)

        # --- (2) Early abort if baseline is too bright (still disabled as before) ---
        thr_check = self.dynamic_threshold
        if (thr_check is not None) and (thr_check >= 1.0):
            # Finalize timing + max brightness
            with self._lock:
                self.metrics["total_end"] = time.perf_counter()
                self.metrics["max_brightness"] = float(getattr(self.camera, "max_light", 0.0))

            # Compute effective threshold + guard band for logging
            eff_thr = self.effective_threshold if (self.effective_threshold is not None) else self.dynamic_threshold
            guard_val = None
            if (self.effective_threshold is not None) and (self.dynamic_threshold is not None):
                guard_val = float(self.effective_threshold - self.dynamic_threshold)
                self.metrics["guard_band"] = guard_val
                self.metrics["effective_threshold"] = eff_thr

            # Persist the aborted run, but don't let DB errors kill the GUI
            try:
                # New signature (with guard/eff_thr)
                save_run(
                    timestamp_id=timestamp_id,
                    status="ABORTED_SEAL_WARNING",
                    metrics=self.metrics,
                    dynamic_threshold=self.dynamic_threshold,
                    guard=guard_val,
                    eff_thr=eff_thr,
                )
            except TypeError:
                # Backwards-compat: older save_run without guard/eff_thr
                save_run(
                    timestamp_id=timestamp_id,
                    status="ABORTED_SEAL_WARNING",
                    metrics=self.metrics,
                    dynamic_threshold=self.dynamic_threshold,
                )
            except Exception as e:
                # Log to stderr but keep UI alive
                print("Error in save_run during ABORTED_SEAL_WARNING:", repr(e), file=sys.stderr)

            # Clean up camera
            try:
                self.camera.stop()
            except Exception:
                pass
            try:
                self.camera.close_camera()
            except Exception:
                pass

            # Show the yellow "seal box" screen on the Tk thread
            self.root.after(0, self.show_seal_warning_screen)
            return

        # --- (3) Post-baseline: concurrent mist / rotation / camera ---
        mist_duration_s = 3.0
        rotation_revs = 1.0  # 1 full 360° rotation; adjust as needed
        extra_analysis_s = max(2.0, float(ROTATIONS) * float(ROTATION_DELAY_S))

        with self._lock:
            self.metrics["analysis_start"] = time.perf_counter()

        camera_thread = threading.Thread(target=self.camera_loop, daemon=True)
        camera_thread.start()

        mist_thread = threading.Thread(target=self._mist_worker, args=(mist_duration_s,), daemon=True)
        rotation_thread = threading.Thread(target=self._rotation_worker, args=(rotation_revs,), daemon=True)

        mist_thread.start()
        rotation_thread.start()

        mist_thread.join()
        rotation_thread.join()

        time.sleep(extra_analysis_s)

        try:
            self.camera.stop()
        except Exception:
            pass
        camera_thread.join(timeout=1.0)

        with self._lock:
            self.metrics["analysis_end"] = time.perf_counter()

        self.max_light = float(getattr(self.camera, "max_light", 0.0))
        with self._lock:
            self.metrics["max_brightness"] = self.max_light

        thr = self.effective_threshold if (self.effective_threshold is not None) else self.dynamic_threshold
        test_failed = (thr is not None) and (self.max_light > thr)
        result_text = "FAILED" if test_failed else "PASSED"
        color = "red" if test_failed else "green"

        heatmap_path, pct = self._finalize_heatmap_and_metrics(timestamp_id, thr)
        self.last_heatmap_path = heatmap_path
        self.last_pct_above_thr = 0.0 if (pct is None) else float(pct)

        with self._lock:
            self.metrics["total_end"] = time.perf_counter()

        eff_thr = thr
        guard_val = None
        if (self.effective_threshold is not None) and (self.dynamic_threshold is not None):
            guard_val = self.effective_threshold - self.dynamic_threshold

        save_run(
            timestamp_id=timestamp_id,
            status=result_text,
            metrics=self.metrics,
            dynamic_threshold=self.dynamic_threshold,
            guard=guard_val,
            eff_thr=eff_thr
        )

        self.root.after(0, lambda: self.show_result_screen(result_text, color))

    def camera_loop(self):
        """
        Post-baseline measurement loop:
        - reads frames
        - updates last_frame for preview
        - tracks max_light across frames
        - accumulates per-pixel max-projection into self._heatmap_max
        - captures time to first threshold exceed (if any)
        """
        try:
            self.camera.start()  # ensures _running=True and camera opened
            while getattr(self.camera, "_running", False):
                try:
                    frame = self.camera.read_frame()
                    self.camera.last_frame = frame

                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)

                    if self._heatmap_max is None:
                        self._heatmap_max = gray.copy()
                    else:
                        np.maximum(self._heatmap_max, gray, out=self._heatmap_max)

                    lv = float(np.mean(gray))

                    if lv > self.camera.max_light:
                        self.camera.max_light = lv

                    with self._lock:
                        self.metrics["frames_total"] += 1
                        self.metrics["frames_analysis"] += 1
                        thr = self.effective_threshold
                        if (thr is not None) and (self.metrics["first_exceed_time"] is None) and (lv > thr):
                            if self.metrics["analysis_start"] is not None:
                                self.metrics["first_exceed_time"] = time.perf_counter() - self.metrics["analysis_start"]
                    time.sleep(0.05)
                except Exception:
                    with self._lock:
                        self.metrics["read_errors"] += 1
                    time.sleep(0.1)
        finally:
            try:
                self.camera.close_camera()
            except Exception:
                pass

    # ---------- heatmap & contamination helpers ----------
    def _sanitize_id_for_filename(self, timestamp_id: str) -> str:
        return timestamp_id.replace(":", "-").replace(" ", "_").replace(".", "-")

    def _finalize_heatmap_and_metrics(self, timestamp_id: str, pixel_threshold: float):
        heatmap_path = None
        pct = 0.0

        try:
            if self._heatmap_max is None:
                with self._lock:
                    self.metrics["heatmap_png_path"] = None
                    self.metrics["pct_frame_above_threshold"] = 0.0
                return None, 0.0

            app_dir = os.path.dirname(__file__)
            data_dir = os.path.join(app_dir, "data")
            out_dir = os.path.join(data_dir, "heatmaps")
            os.makedirs(out_dir, exist_ok=True)

            safe_id = self._sanitize_id_for_filename(timestamp_id)
            heatmap_path = os.path.join(out_dir, f"heatmap_{safe_id}.png")

            maxproj = self._heatmap_max
            mn = float(np.min(maxproj))
            mx = float(np.max(maxproj))
            if mx > mn:
                norm = (maxproj - mn) * (255.0 / (mx - mn))
            else:
                norm = np.zeros_like(maxproj, dtype=np.float32)

            norm_u8 = np.clip(norm, 0, 255).astype(np.uint8)
            colored = cv2.applyColorMap(norm_u8, cv2.COLORMAP_JET)

            cv2.imwrite(heatmap_path, colored)

            if pixel_threshold is not None:
                over = np.count_nonzero(maxproj > float(pixel_threshold))
                total = maxproj.size
                pct = (100.0 * over / total) if (total > 0 and over > 0) else 0.0
            else:
                pct = 0.0

            with self._lock:
                self.metrics["heatmap_png_path"] = heatmap_path
                self.metrics["pct_frame_above_threshold"] = pct
        except Exception:
            heatmap_path = None
            pct = 0.0
            with self._lock:
                self.metrics["heatmap_png_path"] = None
                self.metrics["pct_frame_above_threshold"] = 0.0

        return heatmap_path, pct

    # ---------- entry ----------
    def mainloop(self):
        self.root.mainloop()


def main():
    root = tk.Tk()
    app = TestApp(root)
    app.mainloop()


if __name__ == "__main__":
    main()

