#!/usr/bin/env python3
import requests
import os
import time
from datetime import datetime

# Скрипту надо дать права на исполнение (chmod +x ..../safe_shutdown.py)
# Установить зависимости в систему (sudo apt install -y python3-requests jq)
# Разрешить запускать shutdown без sudo пароля (echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee -a /etc/sudoers)


# Конфигурация
SOCKET_IP = "192.168.31.132"
KLIPPER_API = "http://localhost:7125"
TEMP_THRESHOLD = 50
CHECK_INTERVAL = 15  # Проверка температур каждые 15 сек
SHUTDOWN_DELAY = 5   # Задержка перед выключением RPi
RETRY_INTERVAL = 60  # Интервал между попытками выключения розетки

def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")

def get_heater_temp(heater):
    """Получение температур через Moonraker API"""
    try:
        response = requests.get(
            f"{KLIPPER_API}/printer/objects/query?{heater}",
            timeout=5
        )
        return response.json()['result']['status'][heater]['temperature']
    except Exception as e:
        log(f"Ошибка получения температуры {heater}: {str(e)}")
        return 999  # Возвращаем значение выше порога

def wait_for_cooldown():
    """Ожидание остывания сопла и стола"""
    while True:
        extruder = get_heater_temp("extruder")
        bed = get_heater_temp("heater_bed")
        
        if extruder <= TEMP_THRESHOLD:
            log(f"Температуры в норме: Сопло={extruder}°C, Стол={bed}°C")
            return True
            
        log(f"Ожидание остывания: Сопло={extruder}°C, Стол={bed}°C")
        time.sleep(CHECK_INTERVAL)

def power_off_socket():
    """Попытка выключения розетки"""
    attempt = 0
    while True:
        attempt += 1
        try:
            response = requests.get(
                f"http://{SOCKET_IP}/cm",
                params={"cmnd": "Backlog Delay 300; Power Off"},
                timeout=10
            )
            if response.status_code == 200:
                log(f"Розетка получила команду на отключение (попытка {attempt})")
                return True
            log(f"Ошибка HTTP: {response.status_code}")
        except Exception as e:
            log(f"Ошибка соединения: {str(e)}")
            
        log(f"Повтор через {RETRY_INTERVAL} сек... (попытка {attempt})")
        time.sleep(RETRY_INTERVAL)

def disable_heaters():
    """Отключение нагрева стола и сопла"""
    try:
        requests.post(
            f"{KLIPPER_API}/printer/gcode/script",
            json={"script": "TURN_OFF_HEATERS"},
            timeout=5
        )
        log("Нагрев сопла и стола отключён.")
    except Exception as e:
        log(f"Ошибка при отключении нагревателей: {str(e)}")
					

def main():
    log("=== ЗАПУСК ПРОЦЕДУРЫ ВЫКЛЮЧЕНИЯ ===")
    # Шаг 1: Отключить нагрев
    disable_heaters()

    # Шаг 2: Ожидание остывания
    if wait_for_cooldown():
        # Шаг 3: Выключение розетки
        if power_off_socket():
            # Шаг 4: Выключение RPi
            log(f"Выключение RPi через {SHUTDOWN_DELAY} сек...")
            time.sleep(SHUTDOWN_DELAY)
            os.system("sudo shutdown -h now")

if __name__ == "__main__":
    main()