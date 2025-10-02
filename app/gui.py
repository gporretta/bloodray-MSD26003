#!/usr/bin/env python3
# gui.py

import sys
import time
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from PIL import Image, ImageTk
import cv2

from app.config import (
    WINDOW_GEOMETRY, BG_DARK, ROTATIONS, ROTATION_DELAY_S, LIGHT_THRESHOLD
)
from app.motor import StepperMotor
from app.camera import CameraReader
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

        self.camera = CameraReader()
        self.max_light = 0

        # progress animation bookkeeping
        self._progress_after_id = None
        self._animating = False

        # camera preview bookkeeping
        self._camera_preview_id = None
        self._roi_start = None
        self._roi_rect_id = None
        self._temp_roi_canvas = None
        self._frame_wh = None       # (fw, fh) of last native camera frame
        self._scale_xy = (1.0, 1.0) # (sx, sy) canvas->frame scale

        init_db()
        self.show_home_screen()

    # ---------- lifecycle ----------
    def on_close(self):
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
        # unbind any stray handlers on the container/canvas
        self.container.unbind("<Button-1>")

    # ---------- screens ----------
    def show_home_screen(self):
        self.clear_screen()

        # Header (compact enough for 800x480 overall)
        title = tk.Label(
            self.container, text="Tool Test System",
            font=("Arial", 26, "bold"), fg="white", bg=BG_DARK
        )
        title.pack(pady=(18, 8))

        # Name input row
        name_frame = tk.Frame(self.container, bg=BG_DARK)
        name_frame.pack(pady=(6, 12))
        tk.Label(
            name_frame, text="Your Name:", font=("Arial", 16),
            fg="white", bg=BG_DARK
        ).pack(side="left", padx=8)
        self.name_var = tk.StringVar()
        tk.Entry(
            name_frame, textvariable=self.name_var,
            font=("Arial", 14), width=22
        ).pack(side="left", padx=8)

        # Start test button
        self.start_btn = tk.Button(
            self.container, text="START TEST",
            font=("Arial", 22, "bold"),
            bg="#4CAF50", fg="white", activebackground="#45a049",
            relief="flat", command=self.start_test_thread
        )
        self.start_btn.pack(pady=(10, 12), ipadx=36, ipady=16)
        self.start_btn.config(state="normal")

        # ROI button
        tk.Button(
            self.container, text="SET CAMERA ROI",
            font=("Arial", 18, "bold"),
            bg="#FF9800", fg="white", activebackground="#F57C00",
            relief="flat", command=self.show_roi_selection_screen
        ).pack(pady=6, ipadx=18, ipady=10)

        # Export button
        tk.Button(
            self.container, text="EXPORT TO EXCEL",
            font=("Arial", 18, "bold"),
            bg="#2196F3", fg="white", activebackground="#1976D2",
            relief="flat", command=export_to_excel
        ).pack(pady=(6, 12), ipadx=18, ipady=10)

        # Footer hint
        hint = tk.Label(
            self.container,
            text="Note: ROI is scaled to fit preview but saved in native camera pixels.",
            font=("Arial", 10), fg="#CCCCCC", bg=BG_DARK
        )
        hint.pack(pady=(6, 0))

    def show_progress_screen(self):
        self.clear_screen()
        self.progress_label = tk.Label(
            self.container, text="Test in Progress",
            font=("Arial", 34, "bold"), fg="black", bg="white"
        )
        self.progress_label.pack(expand=True, fill="both")
        self.progress_dots = 0
        self.start_progress_animation()

    def show_result_screen(self, text, color):
        self.clear_screen()
        result_label = tk.Label(
            self.container, text=text, font=("Arial", 44, "bold"),
            fg="white", bg=color
        )
        result_label.pack(expand=True, fill="both")

        current_time = datetime.now().strftime("%H:%M:%S")
        time_label = tk.Label(
            self.container, text=f"Test completed: {current_time}",
            font=("Arial", 14, "bold"), fg="white", bg=color
        )
        time_label.place(relx=0.99, rely=0.02, anchor="ne")

        # Click anywhere to go home
        result_label.bind("<Button-1>", lambda e: self.show_home_screen())

    def show_roi_selection_screen(self):
        self.clear_screen()

        # ---- Header (compact) ----
        title = tk.Label(
            self.container, text="Select Region of Interest",
            font=("Arial", 18, "bold"), fg="white", bg=BG_DARK
        )
        title.pack(pady=(6, 2))

        instructions = tk.Label(
            self.container,
            text="Click and drag to select the area to monitor.",
            font=("Arial", 11), fg="white", bg=BG_DARK
        )
        instructions.pack(pady=(0, 6))

        # ---- Preview canvas (scaled to fit 800x480 layout) ----
        # 720x320 keeps headroom for header+buttons.
        self.preview_w = 720
        self.preview_h = 320
        self.camera_canvas = tk.Canvas(
            self.container, width=self.preview_w, height=self.preview_h,
            bg="black", highlightthickness=0
        )
        self.camera_canvas.pack(pady=4)

        # Bind mouse events for ROI selection
        self.camera_canvas.bind("<ButtonPress-1>", self.on_roi_mouse_down)
        self.camera_canvas.bind("<B1-Motion>", self.on_roi_mouse_drag)
        self.camera_canvas.bind("<ButtonRelease-1>", self.on_roi_mouse_up)

        # ---- Buttons row (compact) ----
        button_frame = tk.Frame(self.container, bg=BG_DARK)
        button_frame.pack(pady=6)

        tk.Button(
            button_frame, text="CONFIRM ROI",
            font=("Arial", 13, "bold"),
            bg="#4CAF50", fg="white", activebackground="#45a049",
            relief="flat", command=self.confirm_roi
        ).pack(side="left", padx=8, ipadx=14, ipady=6)

        tk.Button(
            button_frame, text="BACK",
            font=("Arial", 13, "bold"),
            bg="#757575", fg="white", activebackground="#616161",
            relief="flat", command=self.show_home_screen
        ).pack(side="left", padx=8, ipadx=14, ipady=6)

        # Reset preview/scale state and start camera preview
        self._frame_wh = None
        self._scale_xy = (1.0, 1.0)
        self._temp_roi_canvas = None
        self.start_camera_preview()

    # ---------- ROI selection handlers ----------
    def on_roi_mouse_down(self, event):
        self._roi_start = (event.x, event.y)
        if self._roi_rect_id:
            self.camera_canvas.delete(self._roi_rect_id)
            self._roi_rect_id = None

    def on_roi_mouse_drag(self, event):
        if not self._roi_start:
            return
        if self._roi_rect_id:
            self.camera_canvas.delete(self._roi_rect_id)
        x1, y1 = self._roi_start
        # Clamp to canvas bounds
        x2 = max(0, min(event.x, self.preview_w))
        y2 = max(0, min(event.y, self.preview_h))
        self._roi_rect_id = self.camera_canvas.create_rectangle(
            x1, y1, x2, y2, outline="lime", width=2
        )

    def on_roi_mouse_up(self, event):
        if not self._roi_start:
            return
        x1, y1 = self._roi_start
        # Clamp to canvas bounds
        x2 = max(0, min(event.x, self.preview_w))
        y2 = max(0, min(event.y, self.preview_h))

        x = int(min(x1, x2))
        y = int(min(y1, y2))
        w = int(abs(x2 - x1))
        h = int(abs(y2 - y1))

        if w > 5 and h > 5:
            self._temp_roi_canvas = (x, y, w, h)
            print(f"[DEBUG] ROI (canvas): x={x}, y={y}, w={w}, h={h}")
        else:
            print("[WARNING] ROI too small, ignored")

    def confirm_roi(self):
        """
        Convert ROI from canvas-space to camera native frame-space
        and store it in the CameraReader.
        """
        if not self._temp_roi_canvas or not self._frame_wh:
            print("[WARNING] No ROI selected or no frame available")
            return

        sx, sy = self._scale_xy
        cx, cy, cw, ch = self._temp_roi_canvas

        fx = int(cx / sx)
        fy = int(cy / sy)
        fw = int(cw / sx)
        fh = int(ch / sy)

        # Clamp to frame bounds
        fw = max(1, min(fw, self._frame_wh[0] - fx))
        fh = max(1, min(fh, self._frame_wh[1] - fy))

        try:
            self.camera.set_roi(fx, fy, fw, fh)
            print(f"[DEBUG] ROI (frame): x={fx}, y={fy}, w={fw}, h={fh}")
        except Exception as e:
            print(f"[ERROR] set_roi failed: {e}", file=sys.stderr)

        self.show_home_screen()

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

            frame = self.camera.read_frame()  # BGR
            if frame is None:
                self._camera_preview_id = self.root.after(33, self._update_camera_preview)
                return

            fh, fw = frame.shape[:2]
            self._frame_wh = (fw, fh)

            # Compute scale to fit the chosen canvas exactly
            sx = self.preview_w / float(fw)
            sy = self.preview_h / float(fh)
            self._scale_xy = (sx, sy)

            # Resize for display
            disp = cv2.resize(frame, (self.preview_w, self.preview_h), interpolation=cv2.INTER_AREA)

            # If a persistent ROI (in frame coords) exists, draw it scaled on the display
            if getattr(self.camera, "roi", None):
                rx, ry, rw, rh = self.camera.roi
                rx_s = int(rx * sx); ry_s = int(ry * sy)
                rw_s = int(rw * sx); rh_s = int(rh * sy)
                cv2.rectangle(disp, (rx_s, ry_s), (rx_s + rw_s, ry_s + rh_s), (0, 255, 0), 2)

            # Convert to PhotoImage
            frame_rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            photo = ImageTk.PhotoImage(image=img)

            # Update canvas image
            self.camera_canvas.create_image(0, 0, anchor=tk.NW, image=photo)
            self.camera_canvas.image = photo  # keep reference

            # Keep the live drag rectangle on top
            if self._roi_rect_id:
                self.camera_canvas.tag_raise(self._roi_rect_id)

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
        camera_thread = threading.Thread(target=self.camera_loop, daemon=True)
        camera_thread.start()

        motor = StepperMotor()
        try:
            for r in range(ROTATIONS):
                print(f"[DEBUG] Starting rotation {r+1}/{ROTATIONS}")
                motor.rotate_90()
                print(f"[DEBUG] Rotation {r+1} complete. Waiting {ROTATION_DELAY_S}s before next rotation.")
                # Break wait into chunks for responsiveness
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

        self.max_light = getattr(self.camera, "max_light", 0)
        test_failed = self.max_light > LIGHT_THRESHOLD
        result_text = "FAILED" if test_failed else "PASSED"
        color = "red" if test_failed else "green"
        print(f"[DEBUG] Test finished. Max light observed: {self.max_light}, Result: {result_text}")

        # Tool type removed from UI; pass neutral label to DB
        try:
            operator = self.name_var.get() if hasattr(self, "name_var") else ""
            save_test_result("N/A", operator, result_text)
        except Exception as e:
            print(f"[ERROR] Failed to save result: {e}", file=sys.stderr)

        self.root.after(0, lambda: self.show_result_screen(result_text, color))

    def camera_loop(self):
        # Run continuous camera sampling until stopped by run_test teardown
        try:
            self.camera.loop(sleep_s=0.05)
        except Exception as e:
            print(f"[ERROR] camera_loop: {e}", file=sys.stderr)


def main():
    root = tk.Tk()
    app = TestApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

