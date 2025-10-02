#!/usr/bin/env python3

# ==================== camera.py ====================
import cv2
import numpy as np
from app.config import LIGHT_THRESHOLD

class CameraReader:
    def __init__(self):
        self.camera = None
        self._running = False
        self.max_light = 0
        self.roi = None  # (x, y, w, h)
        
    def open_camera(self):
        """Open the camera device."""
        if self.camera is None or not self.camera.isOpened():
            self.camera = cv2.VideoCapture(0)
            if not self.camera.isOpened():
                raise RuntimeError("Failed to open camera")
            # Set resolution for Raspberry Pi camera
            self.camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        
    def close_camera(self):
        """Release the camera."""
        if self.camera is not None:
            self.camera.release()
            self.camera = None
    
    def read_frame(self):
        """Read a single frame from the camera."""
        if self.camera is None or not self.camera.isOpened():
            self.open_camera()
        
        ret, frame = self.camera.read()
        if not ret:
            raise RuntimeError("Failed to read frame from camera")
        return frame
    
    def set_roi(self, x, y, w, h):
        """Set the region of interest for light detection."""
        self.roi = (x, y, w, h)
        print(f"[DEBUG] ROI set to: x={x}, y={y}, w={w}, h={h}")
    
    def measure_light_in_roi(self, frame):
        """
        Measure light intensity in the ROI.
        Returns the mean brightness value (0-255).
        """
        if self.roi is None:
            # If no ROI set, use entire frame
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            return np.mean(gray)
        
        x, y, w, h = self.roi
        roi_frame = frame[y:y+h, x:x+w]
        gray_roi = cv2.cvtColor(roi_frame, cv2.COLOR_BGR2GRAY)
        return np.mean(gray_roi)
    
    def start(self):
        """Start monitoring."""
        self._running = True
        self.max_light = 0
        self.open_camera()
        print("[DEBUG] Camera monitoring started")
    
    def stop(self):
        """Stop monitoring."""
        self._running = False
        print(f"[DEBUG] Camera monitoring stopped. Max light: {self.max_light}")
    
    def loop(self, sleep_s=0.05):
        """Run the camera polling loop. Call this in a dedicated thread."""
        import time
        self.start()
        try:
            while self._running:
                try:
                    frame = self.read_frame()
                    light_value = self.measure_light_in_roi(frame)
                    
                    if light_value > self.max_light:
                        self.max_light = light_value
                    
                    time.sleep(sleep_s)
                except Exception as e:
                    print(f"[ERROR] Camera read failed: {e}")
                    time.sleep(0.1)
        finally:
            self.close_camera()
            print(f"[DEBUG] Camera monitoring thread stopped. Max light: {self.max_light}")

