#!/usr/bin/env python3
import time
import numpy as np
import cv2

# ---- If you want to lock exposure, try these (backend dependent) ----
LOCK_EXPOSURE = False   # set True if your driver honors it
EXPOSURE_VALUE = -6     # tune per camera/driver; smaller is darker

# ---- ROI in native camera pixels; set to None to use full frame ----
ROI = None  # e.g., ROI = (x, y, w, h)

CAM_INDEX = 0
W = 640
H = 480
SECONDS = 10.0
FPS = 20.0

def roi_gray(frame, roi):
    if roi is None:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    x, y, w, h = roi
    return cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY)

def main():
    cap = cv2.VideoCapture(CAM_INDEX)
    if not cap.isOpened():
        raise RuntimeError("Failed to open camera")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)

    if LOCK_EXPOSURE:
        # These flags vary by driver; sometimes CAP_PROP_AUTO_EXPOSURE=0.25 means manual.
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
        cap.set(cv2.CAP_PROP_EXPOSURE, EXPOSURE_VALUE)
        time.sleep(0.3)

    delay = 1.0 / max(1.0, FPS)
    samples = []

    print("Sampling luminance for ~%.1f s..." % SECONDS)
    t_end = time.time() + SECONDS
    while time.time() < t_end:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue
        gray = roi_gray(frame, ROI)
        samples.append(float(np.mean(gray)))
        time.sleep(delay)

    cap.release()

    arr = np.array(samples, dtype=np.float32)
    if arr.size == 0:
        print("No samples.")
        return

    stats = {
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr, ddof=1) if arr.size > 1 else 0.0),
        "median": float(np.median(arr)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
        "max": float(np.max(arr)),
        "min": float(np.min(arr)),
        "n": int(arr.size),
    }
    print("---- Luminance stats (0..255) over ROI ----")
    for k in ["n","min","mean","median","p95","p99","max","std"]:
        print(f"{k:>6}: {stats[k]:.2f}" if k!="n" else f"{k:>6}: {stats[k]}")
    print("\nPick a static threshold. If you want no safety margin, use ~median.")
    print("If you want slight robustness, choose between p95 and p99 of this run.")
    print("Paste that number into LIGHT_THRESHOLD in your config.")
    
if __name__ == "__main__":
    main()

