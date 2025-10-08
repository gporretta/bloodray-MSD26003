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

# --- Camera & Light Detection ---
# Light threshold for chemiluminescence detection
# Mean brightness value (0-255) above this indicates presence of luminol reaction (FAILED)
LIGHT_THRESHOLD = 0.6  # > threshold => FAILED (adjust based on testing)

# --- Files/Paths ---
DB_FILE = "/var/lib/tool-test/test_results.db"
EXPORT_XLSX = "/var/lib/tool-test/test_results.xlsx"
STATE_DIR = "/var/lib/tool-test"

# --- UI ---
WINDOW_GEOMETRY = "800x480"
BG_DARK = "#222"
