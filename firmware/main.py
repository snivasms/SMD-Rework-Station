# ==========================================================
# SMD Rework Station Firmware
# Part of the ESP32 SMD Rework Station project
# Copyright (c) 2026 Srinivasan M S
# Released under the MIT License
# Version : V5.2
#
# Uses third-party drivers:

# MAX6675       : https://github.com/BetaRavener/micropython-hw-lib 
#       License : MIT
#
# Rotary encoder: https://github.com/MikeTeachman/micropython-rotary
#       License : MIT
#
# LCD drivers   :https://github.com/dhylands/python_lcd
#
#                i2c_lcd.py   I2C LCD driver adapted from  
#                https://github.com/dhylands/python_lcd  
#  (based on esp8266_i2c_lcd.py from the original repository) 
#      License : MIT
#
# ==========================================================

import time
from machine import Pin, PWM, I2C
import ujson as json

# ---------------- PIN CONFIG ----------------
I2C_SCL_PIN = 22
I2C_SDA_PIN = 23
LCD_ADDR = 0x27

SPI_SCK_PIN = 25
SPI_MISO_PIN = 27
MAX_CS_PIN = 26

ENC_CLK_PIN = 32
ENC_DT_PIN = 33
ENC_SW_PIN = 35

HEATER_PWM_PIN = 17        # <-- moved to GPIO17
FAN_PWM_PIN = 4
HEATER_LED_PIN = 18
#FAN_LED_PIN = 5
BUZZER_PIN = 19
HANDLE_REED_PIN = 34

WDT_KICK_PIN = 21          # <-- watchdog output

# ---------------- IMPORT DRIVERS ----------------
from rotary_irq_esp import RotaryIRQ
from i2c_lcd import I2cLcd
from max6675 import MAX6675

# ---------------- HARDWARE INIT ----------------
i2c = I2C(0, scl=Pin(I2C_SCL_PIN), sda=Pin(I2C_SDA_PIN), freq=400000)
lcd = I2cLcd(i2c, LCD_ADDR, 2, 16)

sck = Pin(SPI_SCK_PIN, Pin.OUT)
cs = Pin(MAX_CS_PIN, Pin.OUT)
so = Pin(SPI_MISO_PIN, Pin.IN)
thermo = MAX6675(sck, cs, so)

encoder = RotaryIRQ(
    pin_num_clk=ENC_CLK_PIN,
    pin_num_dt=ENC_DT_PIN,
    min_val=0,
    max_val=1000,
    reverse=False,     
    range_mode=RotaryIRQ.RANGE_UNBOUNDED
)

enc_btn = Pin(ENC_SW_PIN, Pin.IN)

heater_pwm = PWM(Pin(HEATER_PWM_PIN), freq=1000, duty=0)
fan_pwm = PWM(Pin(FAN_PWM_PIN), freq=1000, duty=0)

heater_led = Pin(HEATER_LED_PIN, Pin.OUT) 
buzzer = Pin(BUZZER_PIN, Pin.OUT)

reed_sw = Pin(HANDLE_REED_PIN, Pin.IN)

wdt_pin = Pin(WDT_KICK_PIN, Pin.OUT)
wdt_pin.off()

# ---------------- SCHEDULER INTERVALS ----------------
WDG_INTERVAL     = 250
TEMP_INTERVAL    = 100
PID_INTERVAL     = 100
DISPLAY_INTERVAL = 300
UI_INTERVAL      = 20

PWM_MAX = 1023

# ---------- NOZZLE STORAGE ----------
NOZZLE_FILE = "nozzles.json"
nozzles = {}
current_size = None
entered_size = 6   # default entry size
startup_list = []
startup_index = 0

# ---------------- USER LIMITS ----------------
TEMP_MIN = 50
TEMP_MAX = 450

FAN_MIN_PERCENT = 30
FAN_MAX_PERCENT = 100

HEATER_MIN_PERCENT = 0
HEATER_MAX_PERCENT = 100

# ---------------- DEFAULTS ----------------
set_temp = 250
fan_manual = FAN_MIN_PERCENT

adjust_mode = "TEMP"   # TEMP or FAN 
set_temp = 250
fan_manual = FAN_MIN_PERCENT
fan_on_temp = 100
fan_off_temp = 70
COOLDOWN_DELAY = 30000  # 30 seconds to switch off fan after reaching fan_off_temp

cooldown_active = False
cooldown_start_time = 0

pid_integral = 0.0
pid_prev_error = 0.0
last_pid = 0
last_pid_time = time.ticks_ms()

buzzer_active = False
buzzer_end_time = 0

current_nozzle = None

last_displayed_index = -1

kp = 0.0
ki = 0.0
kd = 0.0

# -------- AUTOTUNE CONFIG --------
CAL_TEMP = 250          # Calibration temperature (mid-range)
CAL_BAND = 8            # ± band for oscillation
CAL_CYCLES = 3          # Number of oscillation cycles
CAL_MAX_TEMP = 320      # Safety abort temperature
RELAY_HIGH = 100        # Heater ON %
RELAY_LOW = 0           # Heater OFF %

# -------- AUTOTUNE STATE --------
AT_STATE = None
at_cycle_count = 0
at_last_cross = 0
at_periods = []
at_max = -999
at_min = 999

# ---------------- UTILS ----------------
def load_pid_for_size(size):
    global kp, ki, kd, fan_manual

    pid = nozzles[str(size)]
    kp = pid["kp"]
    ki = pid["ki"]
    kd = pid["kd"]
    
    #-----------load fan speed safety---------
    fan_manual = pid.get("fan", FAN_MIN_PERCENT)
      
    if fan_manual < FAN_MIN_PERCENT:
        fan_manual = FAN_MIN_PERCENT 
        
def load_nozzles():
    global nozzles
    try:
        with open(NOZZLE_FILE, "r") as f:
            nozzles = json.load(f)
    except:
    #----------- default if file not found-----------
        nozzles = {
            "0": {"kp": 10.0, "ki": 0.5, "kd": 20.0},   # No nozzle default
            "6": {"kp": 12.0, "ki": 0.6, "kd": 25.0},
            "10": {"kp": 14.0, "ki": 0.7, "kd": 30.0}
        }
        save_nozzles()

def save_nozzles():
    with open(NOZZLE_FILE, "w") as f:
        json.dump(nozzles, f)

def default_pid():
    return {"fan":50,"kp": 12.0, "ki": 0.6, "kd": 25.0}

def build_startup_list():
    global startup_list

    startup_list = []
    startup_list.append("No Nozzle")

    for size in sorted(int(k) for k in nozzles.keys() if int(k) != 0):
        startup_list.append(f"{size} mm")

    startup_list.append("New")


def clamp(x, a, b):
    return max(a, min(b, x))

def wdg_pulse():
    wdt_pin.on()
    time.sleep_ms(5)
    wdt_pin.off()

def set_heater_percent(percent):
    percent = clamp(percent, HEATER_MIN_PERCENT, HEATER_MAX_PERCENT)
    duty = int(percent * PWM_MAX / 100)
    heater_pwm.duty(duty)
    heater_led.value(1 if duty > 0 else 0)
         
def set_fan_percent(percent):
    #----------- Enforce minimum only if fan ON-------
    if percent > 0 and percent < FAN_MIN_PERCENT:
        percent = FAN_MIN_PERCENT

    duty = int(clamp(percent, 0, 100) * PWM_MAX / 100)
    fan_pwm.duty(duty)
    
    
def read_temp_safe():
    try:
        t = thermo.read()
        if t is None or t < 0 or t == 32768:
            return None
        return float(t)
    except:
        return None

def beep(duration_ms=20):
    global buzzer_active, buzzer_end_time
    buzzer.on()
    buzzer_active = True
    buzzer_end_time = time.ticks_add(time.ticks_ms(), duration_ms)

    
# ---------------- BUTTON STATE MACHINE ----------------
btn_state = 1
btn_press_time = 0
BTN_SHORT = 0
BTN_LONG = 0

def update_button():
    global btn_state, btn_press_time, BTN_SHORT, BTN_LONG

    BTN_SHORT = 0
    BTN_LONG = 0

    current = enc_btn.value()

    if current == 0 and btn_state == 1:
        btn_press_time = time.ticks_ms()
        btn_state = 0

    elif current == 1 and btn_state == 0:
        duration = time.ticks_diff(time.ticks_ms(), btn_press_time)
        btn_state = 1
        if duration >= 2000:
            BTN_LONG = 1
        else:
            BTN_SHORT = 1

#---------------------LCD writw------------------
def lcd_write_line(row, text):
    lcd.move_to(0, row)
    lcd.putstr((text + " " * 16)[:16])

# ---------------- STARTUP STATE ----------------
fan_pwm.duty(100)
SYSTEM_STATE = "STARTUP" 
startup_index = 0
last_encoder_val = encoder.value()

load_nozzles()
build_startup_list()
set_fan_percent(100)

# ------------------ MAIN LOOP ----------------
def main_loop():

    global pid_integral, pid_prev_error
    global SYSTEM_STATE, startup_index, current_nozzle
    global last_encoder_val, set_temp
    global last_pid_time
    global buzzer_active, buzzer_end_time
    global last_encoder_val
    global last_displayed_index
    global fan_manual
    global adjust_mode
    global cooldown_active, cooldown_start_time
    global current_size

    last_wdg = time.ticks_ms()
    last_temp = time.ticks_ms()
    last_pid = time.ticks_ms()
    last_display = time.ticks_ms()
    last_ui = time.ticks_ms()

    temp = None

    lcd.clear()
    lcd_write_line(0,"SMD Handle V5.2")     
    time.sleep(1)    

    while True:

        now = time.ticks_ms()
                 
        # ---------------- WATCHDOG ---------------------
         
        if time.ticks_diff(now, last_wdg) >= WDG_INTERVAL:
            wdg_pulse()
            last_wdg = now
         
        # ---------------- TEMPERATURE ----------------
        if time.ticks_diff(now, last_temp) >= TEMP_INTERVAL:
            temp = read_temp_safe()
            last_temp = now
       
        # ---------------- STAND DETECTION ----------------
        in_stand = (reed_sw.value() == 0)
        
        if in_stand:
            pid_integral = 0
            pid_prev_error = 0
            set_heater_percent(0)  
        # --------------------- PID -----------------------
        if SYSTEM_STATE == "RUN" and current_size is not None and not in_stand:
            
            if temp is not None and time.ticks_diff(now, last_pid) >= PID_INTERVAL:
                error = set_temp - temp
                #pid_integral += error
                # Anti-windup clamp
                dt = PID_INTERVAL / 1000.0  # convert ms to seconds
                pid_integral += error * dt 
                pid_integral = clamp(pid_integral, -200, 200)
                print(pid_integral)
                derivative = error - pid_prev_error

                output = kp * error + ki * pid_integral + kd * derivative

                pid_prev_error = error

                output = clamp(output, 0, 100)
                if temp is not None:
                    taper_start = 0.85 * set_temp

                if temp > taper_start:
                    scale = (set_temp - temp) / (set_temp - taper_start)
                    scale = clamp(scale, 0, 1)
                    output *= scale # output = output * scale

                #--------- Fan must be running for heater to operate-------
                fan_running = fan_manual >= FAN_MIN_PERCENT and not in_stand

                if not fan_running:
                    set_heater_percent(0)
                else:
                  #---------- Normal PID heater control--------
                    set_heater_percent(output)
                
                if fan_manual < FAN_MIN_PERCENT:
                    set_heater_percent(0)
                    
                last_pid = now
                       
        # ---------------- UI ----------------
        if time.ticks_diff(now, last_ui) >= UI_INTERVAL:

            update_button()

            current_val = encoder.value()
            delta = current_val - last_encoder_val

    # ---------------- STARTUP ----------------
            if SYSTEM_STATE == "STARTUP":

                if delta != 0:
                    last_encoder_val = current_val

                    if delta > 0:
                        startup_index = (startup_index + 1) % len(startup_list)
                    else:
                        startup_index = (startup_index - 1) % len(startup_list)

                    beep(20)

                if BTN_SHORT:
                    beep(40)

                    selected = startup_list[startup_index]

                    if selected == "New":
                        SYSTEM_STATE = "ENTER_SIZE"
                        entered_size = 6
                    else:
                        if selected == "No Nozzle":
                            current_size = 0
                        else:
                            current_size = int(selected.split()[0])
                            
                        load_pid_for_size(current_size)
                        SYSTEM_STATE = "RUN"
                        adjust_mode = "TEMP"

                        last_encoder_val = encoder.value()

    # ---------------- ENTER SIZE ----------------
            elif SYSTEM_STATE == "ENTER_SIZE":

                if delta != 0:
                    last_encoder_val = current_val

                    if delta > 0:
                        entered_size = min(20, entered_size + 1)
                    else:
                        entered_size = max(2, entered_size - 1)

                    beep(20)

                if BTN_SHORT:
                    beep(40)

                    current_size = entered_size

                    if str(current_size) not in nozzles:
                        nozzles[str(current_size)] = default_pid()
                        save_nozzles()
                        build_startup_list()
                        
                    load_pid_for_size(current_size)  
                    SYSTEM_STATE = "RUN"
                    adjust_mode = "TEMP"
                    last_encoder_val = encoder.value()

    # ---------------- RUN ----------------
            elif SYSTEM_STATE == "RUN":
                
    #-----------------ENCODER READ------------------            
                current_val = encoder.value()
                delta = current_val - last_encoder_val

                if delta != 0:
                    step = 1 if delta > 0 else -1
    #----------------- Update reference immediately-----
                    last_encoder_val = current_val

                    if adjust_mode == "TEMP":
                        set_temp = clamp(set_temp + step, TEMP_MIN, TEMP_MAX)

                    elif adjust_mode == "FAN":
                        fan_manual = clamp(fan_manual + step, FAN_MIN_PERCENT, FAN_MAX_PERCENT)

    #----------------- Save per nozzle-----------------------
                        nozzles[str(current_size)]["fan"] = fan_manual
                        save_nozzles()

                    beep(20)
                
   # ---------------- SHORT PRESS TOGGLE ----------------------
                if BTN_SHORT:
                    adjust_mode = "FAN" if adjust_mode == "TEMP" else "TEMP"
                    
   #---------------- Reset encoder reference to avoid jump
                    last_encoder_val = encoder.value()
                    beep(40)

    # ---------------- LONG PRESS CALIBRATION ----------------
                if BTN_LONG:
                    beep(100)
                    SYSTEM_STATE = "CALIBRATING"
                    AT_STATE = "AT_INIT"
    # ---------------- FAN CONTROL ---------------------------
                if not in_stand:
                    set_fan_percent(fan_manual)
                    cooldown_active = False  # reset when handle lifted
                else:
    #----------- Cooling logic in stand--------------------
                   
                    if temp is not None:
   #------------ Above ON threshold → run fan--------------
                        if temp >= fan_on_temp:
                            set_fan_percent(fan_manual)
                            cooldown_active = False

   #------------ Below OFF threshold → start delayed shutdown--------
                        elif temp < fan_off_temp:

                            if not cooldown_active:
                                cooldown_active = True
                                cooldown_start_time = time.ticks_ms()

                            if time.ticks_diff(time.ticks_ms(), cooldown_start_time) >= COOLDOWN_DELAY:
                                set_fan_percent(0)
                            else:
                                set_fan_percent(fan_manual)

    #---------- Between ON and OFF thresholds → keep running--------
                        else:
                            set_fan_percent(fan_manual)
                                       
    # ---------------- CALIBRATING ----------------
            elif SYSTEM_STATE == "CALIBRATING":
                
                if in_stand:                     
                    set_heater_percent(0)
                    
                
                if temp is not None and temp > CAL_MAX_TEMP:                     
                    set_heater_percent(0)
                    beep(300)
                    SYSTEM_STATE = "RUN"                 
                
                if AT_STATE == "AT_INIT":
                    pid_integral = 0
                    pid_prev_error = 0
                    at_cycle_count = 0
                    at_periods = []
                    at_max = -999
                    at_min = 999
                    at_last_cross = time.ticks_ms()
                    AT_STATE = "AT_HEAT"
                                        
                elif AT_STATE == "AT_HEAT" and not in_stand:
                    set_heater_percent(RELAY_HIGH)
                    
                    if temp is not None:
                        at_max = max(at_max, temp)

                        if temp >= CAL_TEMP + CAL_BAND:
                            AT_STATE = "AT_COOL"     
                
                elif AT_STATE == "AT_COOL":
                    set_heater_percent(RELAY_LOW)
                     
                    if temp is not None:
                        at_min = min(at_min, temp)

                        if temp <= CAL_TEMP - CAL_BAND:

                            now_cross = time.ticks_ms()
                            period = time.ticks_diff(now_cross, at_last_cross) / 1000
                            at_periods.append(period)

                            at_last_cross = now_cross
                            at_cycle_count += 1
                    
                            if at_cycle_count >= CAL_CYCLES:
                                AT_STATE = "AT_DONE"
                            else:
                                AT_STATE = "AT_HEAT"
                
                elif AT_STATE == "AT_DONE":

                    set_heater_percent(0)

                    Tu = sum(at_periods) / len(at_periods)
                    a = (at_max - at_min) / 2

                    if a <= 0:                         
                        beep(300)
                        SYSTEM_STATE = "RUN"
                        
                
                    d = RELAY_HIGH
                    Ku = (4 * d) / (3.1416 * a)

                    new_kp = 0.6 * Ku
                    new_ki = (1.2 * Ku) / Tu
                    new_kd = (0.075 * Ku) * Tu

        #------ Store with fan reference (currently 100%)-----
                    nozzles[str(current_size)] = {
                        "fan": 100,
                        "kp": new_kp,
                        "ki": new_ki,
                        "kd": new_kd
                    }

                    save_nozzles()
                    load_pid_for_size(current_size)

                    pid_integral = 0
                    pid_prev_error = 0                     

                    beep(400)

                    SYSTEM_STATE = "RUN"
                    
        # ---------------- DISPLAY ----------------
        if time.ticks_diff(now, last_display) >= DISPLAY_INTERVAL:

            if SYSTEM_STATE == "STARTUP":
                lcd_write_line(0, "Select Nozzle")
                lcd_write_line(1, "> " + startup_list[startup_index])

            elif SYSTEM_STATE == "ENTER_SIZE":
                lcd_write_line(0, "Enter Size:")
                lcd_write_line(1, f"> {entered_size} mm")

            elif SYSTEM_STATE == "RUN":
                if temp is None:
                    line1 = "Temp: ---"                    
                else:
                    line1 = f"T:{int(temp):3d}C F:{fan_manual:3d}%"
                    
                size_text = "No Nozzle" if current_size == 0 else f"{current_size}mm"
                
                if adjust_mode == "TEMP":
                    line2 = f"S:{set_temp:3d}C {size_text}"
                else:
                    line2 = f"FAN:{fan_manual:3d}% {size_text}"
                    
                lcd_write_line(0, line1)
                lcd_write_line(1, line2)    
                                    
            elif SYSTEM_STATE == "CALIBRATING":
                if in_stand : 
                    lcd_write_line(0, "Lift Handle")
                    lcd_write_line(1, "To Calibrate")
                else:
                    lcd_write_line(0, "Autotuning...")
                    lcd_write_line(1, f"T:{int(temp) if temp else 0}")
                    
                if temp is not None and temp > CAL_MAX_TEMP:
                    lcd_write_line(0, "Calibration")
                    lcd_write_line(1, "ABORT - OverTemp")
                    
            elif SYSTEM_STATE == "AT_DONE":
                if a <= 0:
                    lcd_write_line(0, "Calibration")
                    lcd_write_line(1, "FAILED")
                else:
                    lcd_write_line(0, "Calibration")
                    lcd_write_line(1, "DONE")

            last_display = now
                      
        #-------------------Buzzar-------------------
        if buzzer_active and time.ticks_diff(time.ticks_ms(), buzzer_end_time) >= 0:
            buzzer.off()
            buzzer_active = False
        
        # --------------- Yield CPU -----------------
        time.sleep_ms(10)

# ---------------- START ----------------
try:
    main_loop()
except Exception as e:
    lcd.clear()
    lcd.putstr("CRASH")
    raise

