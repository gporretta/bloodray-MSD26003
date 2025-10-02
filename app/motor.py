import time
import RPi.GPIO as GPIO
from app.config import IN1, IN2, IN3, IN4, WAVE_SEQUENCE, STEP_DELAY, STEPS_PER_90_DEG

class StepperMotor:
    def __init__(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setup([IN1, IN2, IN3, IN4], GPIO.OUT, initial=GPIO.LOW)
        print("[DEBUG] Stepper motor initialized")

    def step_once(self):
        for a, b, c, d in WAVE_SEQUENCE:
            GPIO.output(IN1, a)
            GPIO.output(IN2, b)
            GPIO.output(IN3, c)
            GPIO.output(IN4, d)
            time.sleep(STEP_DELAY)

    def rotate_90(self):
        print(f"[DEBUG] Starting 90-degree rotation ({STEPS_PER_90_DEG} steps)")
        for _ in range(STEPS_PER_90_DEG):
            self.step_once()
        print("[DEBUG] 90-degree rotation complete")

    def cleanup(self):
        GPIO.cleanup()
        print("[DEBUG] GPIO cleaned up")

