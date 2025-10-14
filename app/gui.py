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

        # metrics/timing (kept in-memory, persisted to DB at run end)
        self._lock = threading.Lock()
        self.metrics = self._new_metrics()

        # Initialize DB schema
        init_db()

        self.show_home_screen()

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
        }

    # ---------- lifecycle ----------
    def on_close(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True

        self.stop_progress_animation()
        self.stop_camera_preview()
        try:
            self.camera.stop()
            self.camera.close_camera()
        except Exception:
            pass
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
        # tiny hint + timestamp
        ts = datetime.now().strftime("%H:%M:%S")
        meta_label = tk.Label(
            self.container, text=f"Baseline too bright (≥ 1.0) {ts}",
            font=("Arial", 14, "bold"), fg="black", bg=color
        )
        meta_label.place(relx=0.99, rely=0.02, anchor="ne")
        result_label.bind("<Button-1>", lambda e: self.show_home_screen())

    def show_result_screen(self, text, color):
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
        meta_parts.append(f"Guard: {guard:.2f}" if guard is not None else "Guard: N/A")
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

        result_label.bind("<Button-1>", lambda e: self.show_home_screen())

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
        try:
            if not hasattr(self, 'camera_canvas') or not self.camera_canvas.winfo_exists():
                self.stop_camera_preview()
                return

            frame = getattr(self.camera, "last_frame", None)
            if frame is None:
                # direct read if background loop hasn't produced a frame yet
                try:
                    frame = self.camera.read_frame()
                except Exception:
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

                        # Excel sheet names max 31 chars and cannot contain certain chars
                        safe_name = table[:31].replace(":", "_").replace("/", "_").replace("\\", "_").replace("*", "_").replace("?", "_").replace("[", "(").replace("]", ")")
                        ws = wb.create_sheet(title=safe_name)
                        # write header
                        ws.append(headers)
                        # write rows (convert non-str scalars directly; leave JSON as text)
                        for r in rows:
                            ws.append(list(r))

                    wb.save(xlsx_path)
                    messagebox.showinfo("Export Complete", f"Exported to Excel:\n{xlsx_path}")
                else:
                    # Fallback to CSV (one file per table? keep it simple: export test_runs only if exists)
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
        t = threading.Thread(target=self.run_test, daemon=True)
        t.start()

    def run_test(self):
        """
        1) Determine baseline threshold = p95 of mean-brightness over a short window
           with the box closed (no rotation yet).
        2) If baseline p95 >= 1.0, abort the run and show yellow warning (but still save).
        3) Else, run normal rotation workflow and track MAX light AFTER baseline.
        4) PASS if MAX <= effective threshold; FAIL otherwise.
        """
        # Use a wall-clock timestamp string as the run ID (millisecond precision to avoid collisions)
        timestamp_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        # --- (1) Baseline sampling (no rotation) ---
        BASELINE_SECONDS = 2.0
        SAMPLE_PERIOD_S = 0.05  # 20 Hz
        num_samples = max(5, int(BASELINE_SECONDS / SAMPLE_PERIOD_S))
        samples = []

        with self._lock:
            self.metrics["baseline_start"] = time.perf_counter()

        # ensure camera is open (progress screen already opened it)
        try:
            self.camera.open_camera()
        except Exception:
            pass

        for _ in range(num_samples):
            try:
                frame = self.camera.read_frame()
                # update last_frame so preview stays live
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
            # Fail-safe: if baseline failed, set threshold high so we don't false-fail
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

        # --- (2) Early abort if baseline is too bright ---
        thr_check = self.dynamic_threshold
        if (thr_check is not None) and (thr_check >= 1.0):
            # mark end time for total to record timing even on abort
            with self._lock:
                self.metrics["total_end"] = time.perf_counter()
                # mirror max_brightness field for DB row convenience
                self.metrics["max_brightness"] = float(getattr(self.camera, "max_light", 0.0))

            eff_thr = self.effective_threshold if (self.effective_threshold is not None) else self.dynamic_threshold
            guard_val = None
            if (self.effective_threshold is not None) and (self.dynamic_threshold is not None):
                guard_val = self.effective_threshold - self.dynamic_threshold

            # Persist aborted run
            save_run(
                timestamp_id=timestamp_id,
                status="ABORTED_SEAL_WARNING",
                metrics=self.metrics,
                dynamic_threshold=self.dynamic_threshold,
                guard=guard_val,
                eff_thr=eff_thr
            )

            # Stop any camera activity and avoid analysis/rotation
            try:
                self.camera.stop()
            except Exception:
                pass
            try:
                self.camera.close_camera()
            except Exception:
                pass
            self.root.after(0, self.show_seal_warning_screen)
            return

        # Reset max tracking AFTER baseline
        self.camera.max_light = 0.0
        self.max_light = 0.0

        # --- (3) Start measurement loop (post-baseline) ---
        with self._lock:
            self.metrics["analysis_start"] = time.perf_counter()

        camera_thread = threading.Thread(target=self.camera_loop, daemon=True)
        camera_thread.start()

        motor = StepperMotor()
        try:
            for _ in range(ROTATIONS):
                t0 = time.perf_counter()
                motor.rotate_90()
                t1 = time.perf_counter()
                with self._lock:
                    self.metrics["rotation_time_accum"] += (t1 - t0)
                # wait between rotations
                for _ in range(int(ROTATION_DELAY_S * 10)):
                    if not getattr(self.camera, "_running", True):
                        break
                    time.sleep(0.1)
        finally:
            try:
                motor.cleanup()
            except Exception:
                pass
            try:
                self.camera.stop()
            except Exception:
                pass
            camera_thread.join(timeout=1.0)

        # analysis end
        with self._lock:
            self.metrics["analysis_end"] = time.perf_counter()

        # Collect post-baseline max
        self.max_light = float(getattr(self.camera, "max_light", 0.0))
        with self._lock:
            self.metrics["max_brightness"] = self.max_light

        # Use effective threshold for decision
        thr = self.effective_threshold if (self.effective_threshold is not None) else self.dynamic_threshold
        test_failed = (thr is not None) and (self.max_light > thr)
        result_text = "FAILED" if test_failed else "PASSED"
        color = "red" if test_failed else "green"

        # total end
        with self._lock:
            self.metrics["total_end"] = time.perf_counter()

        # Persist completed run
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

        # Show result screen
        self.root.after(0, lambda: self.show_result_screen(result_text, color))

    def camera_loop(self):
        """
        Post-baseline measurement loop:
        - reads frames
        - updates last_frame for preview
        - tracks max_light across frames
        - captures time to first threshold exceed (if any)
        """
        try:
            self.camera.start()  # ensures _running=True and camera opened
            while getattr(self.camera, "_running", False):
                try:
                    frame = self.camera.read_frame()
                    self.camera.last_frame = frame
                    lv = self.camera.measure_light_in_roi(frame)

                    # update max brightness
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

    # ---------- entry ----------
    def mainloop(self):
        self.root.mainloop()


def main():
    root = tk.Tk()
    app = TestApp(root)
    app.mainloop()


if __name__ == "__main__":
    main()

