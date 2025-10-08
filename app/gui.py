#!/usr/bin/env python3
# gui.py

import sys
import time
import threading
import tkinter as tk
from datetime import datetime
from PIL import Image, ImageTk
import cv2
import numpy as np  # <-- for percentile

from app.config import (
    WINDOW_GEOMETRY, BG_DARK, ROTATIONS, ROTATION_DELAY_S, LIGHT_THRESHOLD  # LIGHT_THRESHOLD unused now
)
from app.motor import StepperMotor
from app.camera import CameraReader
from app.db import init_db, save_test_result, export_to_excel


class TestApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Bloodray Automated Tool Test System")
        self.root.geometry(WINDOW_GEOMETRY)
        self.root.configure(bg=BG_DARK)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.container = tk.Frame(self.root, bg=BG_DARK)
        self.container.pack(fill="both", expand=True)

        self.camera = CameraReader()
        self.max_light = 0.0
        self.dynamic_threshold = None

        # progress animation bookkeeping
        self._progress_after_id = None
        self._animating = False

        # camera preview bookkeeping
        self._camera_preview_id = None
        self.preview_w = None
        self.preview_h = None

        # metrics/timing
        self._lock = threading.Lock()
        self.metrics = self._new_metrics()

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

        tk.Button(
            self.container, text="EXPORT TO EXCEL",
            font=("Arial", 22, "bold"),
            bg="#2196F3", fg="white", activebackground="#1976D2",
            relief="flat", command=export_to_excel
        ).pack(pady=(6, 12), ipadx=18, ipady=10)

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

    def show_result_screen(self, text, color):
        self.clear_screen()
        result_label = tk.Label(
            self.container, text=text, font=("Arial", 44, "bold"),
            fg="white", bg=color
        )
        result_label.pack(expand=True, fill="both")

        # small telemetry line (helps debugging)
        meta = f"Baseline p95: {self.dynamic_threshold:.2f}  |  Max: {self.max_light:.2f}"
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
        except Exception as e:
            print(f"[ERROR] open_camera failed: {e}", file=sys.stderr)
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
        except Exception as e:
            print(f"[ERROR] Camera preview failed: {e}")
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

    # ---------- test flow ----------
    def start_test_thread(self):
        self.start_btn.config(state="disabled")
        self.show_progress_screen()
        print("[DEBUG] Starting test thread")
        # reset metrics per run
        with self._lock:
            self.metrics = self._new_metrics()
            self.metrics["total_start"] = time.perf_counter()
        t = threading.Thread(target=self.run_test, daemon=True)
        t.start()

    def run_test(self):
        """
        1) Determine baseline threshold = p95 of mean-brightness over a short window
           with the box closed (no rotation yet).
        2) Then run the normal rotation workflow and track MAX light AFTER baseline.
        3) PASS if MAX <= threshold; FAIL otherwise.
        """
        # --- (1) Baseline sampling (no rotation) ---
        BASELINE_SECONDS = 2.0
        SAMPLE_PERIOD_S = 0.05  # 20 Hz
        num_samples = max(5, int(BASELINE_SECONDS / SAMPLE_PERIOD_S))
        samples = []

        with self._lock:
            self.metrics["baseline_start"] = time.perf_counter()

        print(f"[DEBUG] Baseline sampling for {BASELINE_SECONDS}s (~{num_samples} frames)")
        # ensure camera is open (progress screen already opened it)
        try:
            self.camera.open_camera()
        except Exception as e:
            print(f"[ERROR] open_camera for baseline failed: {e}", file=sys.stderr)

        for i in range(num_samples):
            try:
                frame = self.camera.read_frame()
                # update last_frame so preview stays live
                self.camera.last_frame = frame
                lv = self.camera.measure_light_in_roi(frame)  # full-frame mean
                samples.append(lv)
                with self._lock:
                    self.metrics["frames_total"] += 1
                    self.metrics["frames_baseline"] += 1
            except Exception as e:
                print(f"[WARN] Baseline frame read failed at i={i}: {e}")
                with self._lock:
                    self.metrics["read_errors"] += 1
            time.sleep(SAMPLE_PERIOD_S)

        with self._lock:
            self.metrics["baseline_end"] = time.perf_counter()

        if not samples:
            # Hard fail-safe: if baseline failed, set threshold high so we don't false-fail
            self.dynamic_threshold = 255.0
            print("[WARN] No baseline samples captured; using threshold=255.0")
            with self._lock:
                self.metrics["baseline_samples"] = []
                self.metrics["baseline_mean"] = None
                self.metrics["baseline_std"] = None
                self.metrics["baseline_p95"] = None
        else:
            self.dynamic_threshold = float(np.percentile(samples, 95))
            b_mean = float(np.mean(samples))
            b_std = float(np.std(samples, ddof=1)) if len(samples) > 1 else 0.0
            print(f"[DEBUG] Baseline p95 threshold: {self.dynamic_threshold:.2f}")
            with self._lock:
                self.metrics["baseline_samples"] = samples
                self.metrics["baseline_mean"] = b_mean
                self.metrics["baseline_std"] = b_std
                self.metrics["baseline_p95"] = self.dynamic_threshold

        # Reset max tracking AFTER baseline
        self.camera.max_light = 0.0
        self.max_light = 0.0

        # --- (2) Start measurement loop (post-baseline) ---
        with self._lock:
            self.metrics["analysis_start"] = time.perf_counter()

        camera_thread = threading.Thread(target=self.camera_loop, daemon=True)
        camera_thread.start()

        motor = StepperMotor()
        try:
            for r in range(ROTATIONS):
                print(f"[DEBUG] Starting rotation {r+1}/{ROTATIONS}")
                t0 = time.perf_counter()
                motor.rotate_90()
                t1 = time.perf_counter()
                with self._lock:
                    self.metrics["rotation_time_accum"] += (t1 - t0)
                print(f"[DEBUG] Rotation {r+1} complete. Waiting {ROTATION_DELAY_S}s before next rotation.")
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

        test_failed = self.max_light > self.dynamic_threshold
        result_text = "FAILED" if test_failed else "PASSED"
        color = "red" if test_failed else "green"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # total end
        with self._lock:
            self.metrics["total_end"] = time.perf_counter()

        # --- (4) Print summary to terminal ---
        self._print_summary(timestamp, result_text)

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
                        # capture time-to-first-exceed if threshold known
                        thr = self.dynamic_threshold
                        if (thr is not None) and (self.metrics["first_exceed_time"] is None) and (lv > thr):
                            if self.metrics["analysis_start"] is not None:
                                self.metrics["first_exceed_time"] = time.perf_counter() - self.metrics["analysis_start"]
                    time.sleep(0.05)
                except Exception as e:
                    print(f"[ERROR] camera_loop read: {e}", file=sys.stderr)
                    with self._lock:
                        self.metrics["read_errors"] += 1
                    time.sleep(0.1)
        finally:
            try:
                self.camera.close_camera()
            except Exception:
                pass

    # ---------- reporting ----------
    def _print_summary(self, timestamp, result_text):
        with self._lock:
            m = dict(self.metrics)  # shallow copy

        # durations (seconds)
        def dur(s, e):
            if s is None or e is None:
                return None
            return max(0.0, e - s)

        total_time = dur(m["total_start"], m["total_end"])
        baseline_time = dur(m["baseline_start"], m["baseline_end"])
        analysis_time = dur(m["analysis_start"], m["analysis_end"])
        rotation_time = m["rotation_time_accum"] or 0.0

        # fps (avoid div-by-zero)
        fps_analysis = (m["frames_analysis"] / analysis_time) if analysis_time and analysis_time > 0 else 0.0
        fps_total = (m["frames_total"] / total_time) if total_time and total_time > 0 else 0.0

        # margin (positive means above threshold → fail)
        margin = None
        if self.dynamic_threshold is not None:
            margin = self.max_light - self.dynamic_threshold

        # summarize
        print("\n================= TEST SUMMARY =================")
        print(f"Timestamp:              {timestamp}")
        print(f"Result:                 {result_text}")
        print("--- Thresholds/Brightness ---")
        print(f"Baseline mean:          {m['baseline_mean']:.2f}" if m['baseline_mean'] is not None else "Baseline mean:          N/A")
        print(f"Baseline std:           {m['baseline_std']:.2f}" if m['baseline_std'] is not None else "Baseline std:           N/A")
        print(f"Baseline (p95):         {self.dynamic_threshold:.2f}" if self.dynamic_threshold is not None else "Baseline (p95):         N/A")
        print(f"Max Brightness:         {self.max_light:.2f}")
        if margin is not None:
            print(f"Margin (max - thr):     {margin:.2f}")
        if m["first_exceed_time"] is not None:
            print(f"Time to first exceed:   {m['first_exceed_time']:.3f} s")
        else:
            print("Time to first exceed:   N/A")
        print("--- Timing ---")
        print(f"Total time:             {total_time:.3f} s" if total_time is not None else "Total time:             N/A")
        print(f"Baseline time:          {baseline_time:.3f} s" if baseline_time is not None else "Baseline time:          N/A")
        print(f"Analysis time:          {analysis_time:.3f} s" if analysis_time is not None else "Analysis time:          N/A")
        print(f"Rotation time (sum):    {rotation_time:.3f} s")
        print("--- Frames ---")
        print(f"Frames (baseline):      {m['frames_baseline']}")
        print(f"Frames (analysis):      {m['frames_analysis']}")
        print(f"Frames (total):         {m['frames_total']}")
        print(f"Read errors:            {m['read_errors']}")
        print(f"FPS (analysis):         {fps_analysis:.2f}")
        print(f"FPS (overall):          {fps_total:.2f}")
        print("================================================\n")

    # ---------- entry ----------
    def mainloop(self):
        self.root.mainloop()


def main():
    root = tk.Tk()
    app = TestApp(root)
    app.mainloop()


if __name__ == "__main__":
    main()

