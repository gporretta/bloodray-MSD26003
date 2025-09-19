import time
import smbus
from config import I2C_BUS, I2C_ADDRESS, ADC_CHANNEL

class ADCReader:
    def __init__(self):
        self.bus = smbus.SMBus(I2C_BUS)
        self._running = False
        self.max_light = 0

    def read_adc_once(self, channel=ADC_CHANNEL):
        if channel < 0 or channel > 7:
            raise ValueError("Channel must be 0-7")
        cmd = 0x84 | (channel << 4)
        self.bus.write_byte(I2C_ADDRESS, cmd)
        return self.bus.read_byte(I2C_ADDRESS)

    def start(self):
        self._running = True
        self.max_light = 0
        print("[DEBUG] ADC monitoring thread started")

    def stop(self):
        self._running = False

    def loop(self, sleep_s=0.05):
        """Run the ADC polling loop. Call this in a dedicated thread."""
        self.start()
        try:
            while self._running:
                try:
                    v = self.read_adc_once()
                    if v > self.max_light:
                        self.max_light = v
                    time.sleep(sleep_s)
                except Exception as e:
                    print(f"[ERROR] ADC read failed: {e}")
                    time.sleep(0.1)
        finally:
            print(f"[DEBUG] ADC monitoring thread stopped. Max light: {self.max_light}")

