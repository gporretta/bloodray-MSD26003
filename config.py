# --- Stepper Motor ---
IN1, IN2, IN3, IN4 = 23, 24, 25, 8
WAVE_SEQUENCE = [
    [1,0,0,0],
    [0,1,0,0],
    [0,0,1,0],
    [0,0,0,1],
]
STEP_DELAY = 0.002
STEPS_PER_90_DEG = 128
ROTATIONS = 4
ROTATION_DELAY_S = 3  # seconds between rotations

# --- ADC ---
I2C_ADDRESS = 0x4B
I2C_BUS = 1
ADC_CHANNEL = 0
LIGHT_THRESHOLD = 60  # > threshold => FAILED

# --- Files/Paths ---
DB_FILE = "/var/lib/tool-test/test_results.db"     # more appropriate for system data
EXPORT_XLSX = "/var/lib/tool-test/test_results.xlsx"

# Ensure export dir exists if you keep using /var/lib
STATE_DIR = "/var/lib/tool-test"

# --- UI ---
WINDOW_GEOMETRY = "800x480"
BG_DARK = "#222"

