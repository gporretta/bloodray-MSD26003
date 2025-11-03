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

from app.config import (
    WINDOW_GEOMETRY, BG_DARK, ROTATIONS, ROTATION_DELAY_S, LIGHT_THRESHOLD  # LIGHT_THRESHOLD unused now
)
from app.motor import StepperMotor
from app.camera import CameraReader
from app.db import init_db, save_run  # DB split: external module

# --- Robust-threshold tunables (mean-domain, 0–255 scale) ---
GUARD_MEAN_ABS = 5.0     # absolute guard band (counts) added to baseline quantile of frame means
GUARD_MEAN_SIGMA = 3.0    # multiplier on baseline std dev of frame means
BASELINE_Q = 99.5         # quantile on per-frame mean for baseline threshold

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
        self.dynamic_threshold = None       # baseline q99.5 of frame means
        self.effective_threshold = None     # baseline q99.5 + guard (mean-domain)

        # progress animation bookkeeping
        self._progress_after_id = None
        self._animating = False

        # camera preview bookkeeping
        self._camera_preview_id = None
        self.preview_w = None
        self.preview_h = None

        # frame grabber (keeps last_frame fresh at all times, incl. mist/rotation)
        self._grabber_thread = None
        self._grabber_running = False

        # analysis loop thread (reads from last_frame; does NOT own the camera)
        self._analysis_thread = None
        self._analysis_running = False

        # heatmap accumulation (per-run)
        self._heatmap_max = None           # np.ndarray (float32), max-projection of grayscale frames
        self.last_heatmap_path = None      # str path to saved heatmap PNG
        self.last_pct_above_thr = None     # float percentage of pixels over effective threshold (telemetry only)

        # metrics/timing (kept in-memory, persisted to DB at run end)
        self._lock = threading.Lock()
        self.metrics = self._new_metrics()

        # GPIO for misting relay (GPIO17, BCM)
        self.GPIO = None
        self.MIST_PIN = 17
        self._init_gpio()

        # Initialize DB schema
        init_db()

        self.show_home_screen()

    # ---------- GPIO ----------
    def _init_gpio(self):
        """Initialize Raspberry Pi GPIO (BCM mode) and set MIST_PIN as output LOW. Safe off-Pi."""
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.MIST_PIN, GPIO.OUT, initial=GPIO.LOW)
            self.GPIO = GPIO
        except Exception:
            self.GPIO = None  # dev machine or unavailable; ignore

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

    # ---------- camera grabber (always-on during a run) ----------
    def _start_frame_grabber(self):
        """Continuously reads frames into self.camera.last_frame so the preview never stops."""
        if self._grabber_running:
            return
        try:
            self.camera.open_camera()
        except Exception:
            pass

        self._grabber_running = True

        def _loop():
            while self._grabber_running:
                try:
                    frame = self.camera.read_frame()
                    # make available to UI and analysis
                    self.camera.last_frame = frame
                except Exception:
                    time.sleep(0.02)
                    continue
                # target ~30 fps if possible
                time.sleep(0.01)

        self._grabber_thread = threading.Thread(target=_loop, daemon=True)
        self._grabber_thread.start()

    def _stop_frame_grabber(self):
        self._grabber_running = False
        t = self._grabber_thread
        self._grabber_thread = None
        if t is not None:
            t.join(timeout=1.0)

        try:
            self.camera.close_camera()
        except Exception:
            pass

    # ---------- analysis loop (reads from last_frame; does not touch camera I/O) ----------
    def _start_analysis_loop(self, duration_s: float):
        if self._analysis_running:
            return
        self._analysis_running = True
        with self._lock:
            self.metrics["analysis_start"] = time.perf_counter()

        def _loop():
            deadline = time.perf_counter() + duration_s
            try:
                while self._analysis_running and time.perf_counter() < deadline:
                    frame = getattr(self.camera, "last_frame", None)
                    if frame is None:
                        time.sleep(0.01)
                        continue

                    # grayscale for metrics/heatmap
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)

                    # update heatmap max projection (telemetry)
                    if self._heatmap_max is None:
                        self._heatmap_max = gray.copy()
                    else:
                        np.maximum(self._heatmap_max, gray, out=self._heatmap_max)

                    # per-frame mean for decision logic
                    lv = float(np.mean(gray))

                    # update max brightness (mean)
                    if lv > getattr(self.camera, "max_light", 0.0):
                        self.camera.max_light = lv

                    with self._lock:
                        self.metrics["frames_total"] += 1
                        self.metrics["frames_analysis"] += 1
                        thr = self.effective_threshold
                        if (thr is not None) and (self.metrics["first_exceed_time"] is None) and (lv > thr):
                            if self.metrics["analysis_start"] is not None:
                                self.metrics["first_exceed_time"] = time.perf_counter() - self.metrics["analysis_start"]

                    time.sleep(0.05)
            finally:
                with self._lock:
                    self.metrics["analysis_end"] = time.perf_counter()
                self._analysis_running = False

        self._analysis_thread = threading.Thread(target=_loop, daemon=True)
        self._analysis_thread.start()

    def _stop_analysis_loop(self):
        self._analysis_running = False
        t = self._analysis_thread
        self._analysis_thread = None
        if t is not None:
            t.join(timeout=1.0)

    # ---------- helper for mist+rotation (does NOT touch camera) ----------
    def _mist_and_rotate(self, motor, seconds: float, revolutions: float = 1.0):
        """
        Drive GPIO17 HIGH while rotating the stepper ~360° over `seconds`, then set LOW.
        Rotation is approximated with four 90° moves spaced evenly across the window.
        Camera preview continues because the frame grabber is independent.
        """
        segments = max(1, int(round(4 * revolutions)))      # 4 segments per revolution
        interval = float(seconds) / segments if segments > 0 else float(seconds)

        start = time.perf_counter()
        self._mist_on()
        try:
            for _ in range(segments):
                t0 = time.perf_counter()
                try:
                    motor.rotate_90()
                except Exception:
                    pass
                elapsed = time.perf_counter() - t0
                remaining = interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)
            # pad to full duration if needed
            total_elapsed = time.perf_counter() - start
            tail = seconds - total_elapsed
            if tail > 0:
                time.sleep(tail)
        finally:
            self._mist_off()

    # ---------- metrics scaffold ----------
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

            # brightness & detection (mean-domain)
            "baseline_means": [],
            "baseline_mean": None,
            "baseline_std": None,
            "baseline_q995": None,
            "guard_band_mean": None,
            "effective_threshold_mean": None,
            "first_exceed_time": None,   # seconds since analysis_start
            "max_brightness": 0.0,       # max frame mean observed during analysis

            # heatmap & contamination (telemetry)
            "heatmap_png_path": None,
            "pct_frame_above_threshold": None,
        }

    # ---------- lifecycle ----------
    def on_close(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True

        self.stop_progress_animation()
        self.stop_camera_preview()

        # stop test-time loops if any
        self._stop_analysis_loop()
        self._stop_frame_grabber()

        try:
            self.camera.stop()
        except Exception:
            pass
        try:
            self.camera.close_camera()
        except Exception:
            pass

        self._gpio_cleanup()
        self.root.after(50, self.root.destroy)

    def clear_screen(self):
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
        meta_parts = []
        if self.dynamic_threshold is not None:
            meta_parts.append(f"Baseline q{BASELINE_Q:.1f}(mean): {self.dynamic_threshold:.2f}")
        meta_parts.append(f"Eff Thr(mean): {self.effective_threshold:.2f}" if self.effective_threshold is not None else "Eff Thr(mean): N/A")
        meta_parts.append(f"Max(mean): {self.max_light:.2f}")
        if self.last_pct_above_thr is not None:
            meta_parts.append(f"Contam %: {self.last_pct_above_thr:.1f}%")
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

        header = tk.Frame(self.container, bg=bg)
        header.pack(fill="x", pady=(8, 4))
        title_lbl = tk.Label(
            header, text="Contamination Heatmap",
            font=("Arial", 24, "bold"), fg="white", bg=bg
        )
        title_lbl.pack(side="left", padx=(12, 8))

        if self.last_pct_above_thr is not None and self.effective_threshold is not None:
            sub = f"Frame Contam %: {self.last_pct_above_thr:.1f}%  (thr mean: {self.effective_threshold:.2f})"
        else:
            sub = "Heatmap unavailable"
        sub_lbl = tk.Label(header, text=sub, font=("Arial", 14, "bold"), fg="#CCCCCC", bg=bg)
        sub_lbl.pack(side="left")

        canvas = tk.Canvas(self.container, bg="black", highlightthickness=0, width=780, height=440, cursor="hand2")
        canvas.pack(pady=(4, 12))

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

    # ---------- camera preview (UI) ----------
    def start_camera_preview(self):
        # The preview uses self.camera.last_frame which is kept fresh by the grabber.
        self._update_camera_preview()

    def stop_camera_preview(self):
        if self._camera_preview_id is not None:
            try:
                self.root.after_cancel(self._camera_preview_id)
            except Exception:
                pass
            self._camera_preview_id = None

    def _update_camera_preview(self):
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
            self._camera_preview_id = self.root.after(50, self._update_camera_preview)

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

        with self._lock:
            self.metrics = self._new_metrics()
            self.metrics["total_start"] = time.perf_counter()

        # reset per-run telemetry
        self._heatmap_max = None
        self.last_heatmap_path = None
        self.last_pct_above_thr = None

        # ensure preview keeps flowing for entire run
        self._start_frame_grabber()

        t = threading.Thread(target=self.run_test, daemon=True)
        t.start()

    def run_test(self):
        """
        1) Determine baseline threshold on frame means: q99.5 + guard (no rotation).
        2) Misting phase: set GPIO17 HIGH for 3s and rotate ~360° during those 3s (preview continues).
        3) Analysis phase: run collection (NO rotation), track max frame mean.
        4) Compute telemetry heatmap + contamination %.
        5) Persist run and show PASS/FAIL based on mean-domain threshold.
        """
        timestamp_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        # --- (1) Baseline sampling (no rotation, preview active via grabber) ---
        BASELINE_SECONDS = 2.0
        SAMPLE_PERIOD_S = 0.05  # 20 Hz
        num_samples = max(5, int(BASELINE_SECONDS / SAMPLE_PERIOD_S))

        baseline_means = []

        with self._lock:
            self.metrics["baseline_start"] = time.perf_counter()

        for _ in range(num_samples):
            try:
                frame = getattr(self.camera, "last_frame", None)
                if frame is None:
                    time.sleep(SAMPLE_PERIOD_S)
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
                m = float(np.mean(gray))
                baseline_means.append(m)

                with self._lock:
                    self.metrics["frames_total"] += 1
                    self.metrics["frames_baseline"] += 1
            except Exception:
                with self._lock:
                    self.metrics["read_errors"] += 1
            time.sleep(SAMPLE_PERIOD_S)

        with self._lock:
            self.metrics["baseline_end"] = time.perf_counter()

        if not baseline_means:
            # Fail-safe: push threshold high to avoid false-fail
            self.dynamic_threshold = 255.0
            self.effective_threshold = 255.0
            with self._lock:
                self.metrics["baseline_means"] = []
                self.metrics["baseline_mean"] = None
                self.metrics["baseline_std"] = None
                self.metrics["baseline_q995"] = None
                self.metrics["guard_band_mean"] = None
                self.metrics["effective_threshold_mean"] = 255.0
        else:
            b_means = np.array(baseline_means, dtype=np.float32)
            dyn_mean = float(np.percentile(b_means, BASELINE_Q))
            std_mean = float(np.std(b_means, ddof=1)) if b_means.size > 1 else 0.0
            guard_mean = max(GUARD_MEAN_ABS, GUARD_MEAN_SIGMA * std_mean)
            eff_thr_mean = float(np.clip(dyn_mean + guard_mean, 0.0, 255.0))

            self.dynamic_threshold = dyn_mean
            self.effective_threshold = eff_thr_mean

            with self._lock:
                self.metrics["baseline_means"] = baseline_means
                self.metrics["baseline_mean"] = float(np.mean(b_means))
                self.metrics["baseline_std"] = std_mean
                self.metrics["baseline_q995"] = dyn_mean
                self.metrics["guard_band_mean"] = float(guard_mean)
                self.metrics["effective_threshold_mean"] = eff_thr_mean

        # --- (2) Misting phase with 360° rotation during the 3-second pulse ---
        try:
            motor = StepperMotor()
        except Exception:
            motor = None
        try:
            if motor is not None:
                self._mist_and_rotate(motor, seconds=3.0, revolutions=1.0)  # 360° over 3s
            else:
                self._mist_on()
                time.sleep(3.0)
                self._mist_off()
        finally:
            try:
                if motor is not None:
                    motor.cleanup()
            except Exception:
                pass

        # --- (3) Analysis phase (NO rotation): run analysis loop over live frames ---
        analysis_duration_s = max(2.0, float(ROTATIONS) * float(ROTATION_DELAY_S))
        # reset max tracker
        self.camera.max_light = 0.0
        self.max_light = 0.0

        self._start_analysis_loop(duration_s=analysis_duration_s)

        # Wait for analysis to finish
        self._stop_analysis_loop()

        # Collect post-baseline max frame mean
        self.max_light = float(getattr(self.camera, "max_light", 0.0))
        with self._lock:
            self.metrics["max_brightness"] = self.max_light

        # PASS/FAIL: mean-domain threshold
        thr = self.effective_threshold
        test_failed = (thr is not None) and (self.max_light > thr)
        result_text = "FAILED" if test_failed else "PASSED"
        color = "red" if test_failed else "green"

        # --- (4) Compute telemetry heatmap + contamination % ---
        heatmap_path, pct = self._finalize_heatmap_and_metrics(timestamp_id, thr)
        self.last_heatmap_path = heatmap_path
        self.last_pct_above_thr = 0.0 if (pct is None) else float(pct)

        # total end
        with self._lock:
            self.metrics["total_end"] = time.perf_counter()

        # Persist completed run
        save_run(
            timestamp_id=timestamp_id,
            status=result_text,
            metrics=self.metrics,
            dynamic_threshold=self.dynamic_threshold,  # mean-domain q99.5
            guard=(self.effective_threshold - self.dynamic_threshold) if (self.effective_threshold is not None and self.dynamic_threshold is not None) else None,
            eff_thr=self.effective_threshold
        )

        # Show result screen (tap -> heatmap)
        self.root.after(0, lambda: self.show_result_screen(result_text, color))

        # NOTE: keep the grabber running while result/heatmap screens are up?
        # We'll stop it when returning Home or on close to preserve preview on result screens.
        # If you want to stop immediately after analysis, uncomment:
        # self._stop_frame_grabber()

    # ---------- heatmap & contamination helpers ----------
    def _sanitize_id_for_filename(self, timestamp_id: str) -> str:
        # "2025-10-30 12:34:56.789" -> "2025-10-30_12-34-56-789"
        return timestamp_id.replace(":", "-").replace(" ", "_").replace(".", "-")

    def _finalize_heatmap_and_metrics(self, timestamp_id: str, pixel_threshold: float):
        """
        Build a colored heatmap PNG from the per-pixel max-projection and compute
        the % of frame with pixels over 'pixel_threshold' (telemetry).
        Returns (path, pct). Updates metrics accordingly.
        """
        heatmap_path = None
        pct = 0.0

        try:
            if self._heatmap_max is None:
                with self._lock:
                    self.metrics["heatmap_png_path"] = None
                    self.metrics["pct_frame_above_threshold"] = 0.0
                return None, 0.0

            # Create dirs: app/data/heatmaps next to this file
            app_dir = os.path.dirname(__file__)
            data_dir = os.path.join(app_dir, "data")
            out_dir = os.path.join(data_dir, "heatmaps")
            os.makedirs(out_dir, exist_ok=True)

            safe_id = self._sanitize_id_for_filename(timestamp_id)
            heatmap_path = os.path.join(out_dir, f"heatmap_{safe_id}.png")

            # Normalize max-projection to 0..255 uint8 for colormap
            maxproj = self._heatmap_max
            mn = float(np.min(maxproj))
            mx = float(np.max(maxproj))
            if mx > mn:
                norm = (maxproj - mn) * (255.0 / (mx - mn))
            else:
                norm = np.zeros_like(maxproj, dtype=np.float32)

            norm_u8 = np.clip(norm, 0, 255).astype(np.uint8)
            colored = cv2.applyColorMap(norm_u8, cv2.COLORMAP_JET)

            # Save PNG
            cv2.imwrite(heatmap_path, colored)

            # Compute % of pixels over threshold using original (non-normalized) scale
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

