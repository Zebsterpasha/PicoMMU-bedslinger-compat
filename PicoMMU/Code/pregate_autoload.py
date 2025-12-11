# pregate_autoload.py
# Background autoload plugin for Klipper
# Monitors MMU pregate sensors and auto-loads filament when printer is idle

import threading
import time
import logging

class PregateAutoLoad:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.sensor_names = [
            "mmu_pregate_0",
            "mmu_pregate_1",
            "mmu_pregate_2",
            "mmu_pregate_3",
        ]

        self.sensors = []
        self.last_state = []
        for name in self.sensor_names:
            try:
                sensor = self.printer.lookup_object(f"filament_switch_sensor {name}")
                self.sensors.append(sensor)
                # читаем текущее состояние, чтобы не реагировать на уже вставленный филамент
                st = sensor.get_status(self.reactor.monotonic())
                val = 1 if st.get('filament_detected', False) else 0
                self.last_state.append(val)
                logging.info(f"[PregateAutoLoad] Initial state {name}: {val}")
            except Exception as e:
                logging.info(f"[PregateAutoLoad] Can't find sensor '{name}': {e}")
                self.sensors.append(None)
                self.last_state.append(0)

        self.cmd_queue = []
        self.running = True
        self.poll_interval = 0.25
        self.queue_processor_registered = False
        self.mmu_ready = False  # чтобы не пихать до инициализации

        # следим за готовностью MMU — через событие printer.objects.subscribed
        try:
            self.gcode.register_command("SP_HOME_DONE", self.cmd_home_done)
        except Exception:
            pass

        logging.info("[PregateAutoLoad] Pregate sensors initialized.")
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()
        logging.info("[PregateAutoLoad] Background filament autoload started.")

    def cmd_home_done(self, gcmd):
        """Вызывается из макроса SP_HOME_DONE, когда MMU полностью инициализирован."""
        self.mmu_ready = True
        logging.info("[PregateAutoLoad] MMU reported ready (SP_HOME_DONE).")


    def _run_loop(self):
        # подождём стартовой инициализации
        # Ждём пока MMU не станет готовой
        while not self.mmu_ready:
            time.sleep(self.poll_interval)

        logging.info("[PregateAutoLoad] Main loop started (MMU ready).")


        while self.running:
            try:
                # проверяем состояние принтера: если не в idle/ready/standby - не выполняем автозагрузку
                try:
                    ps = self.printer.lookup_object("print_stats")
                    state = ps.get_status(self.reactor.monotonic()).get('state', '').lower()
                except Exception:
                    # fallback: использовать printer.get_state_message() (старые версии)
                    sm = self.printer.get_state_message()
                    # printer.get_state_message() может возвращать строку или кортеж, постараемся безопасно извлечь
                    if isinstance(sm, (list, tuple)) and len(sm) > 0:
                        state = str(sm[0]).lower()
                    else:
                        state = str(sm).lower()

                # разрешаем автозагрузку только если принтер в покое
                allowed_states = ("idle", "ready", "standby", "paused")
                if state not in allowed_states:
                    time.sleep(self.poll_interval)
                    continue

                # опрашиваем сенсоры pregate
                for lane, sensor in enumerate(self.sensors):
                    if sensor is None:
                        # сенсор не найден — пропускаем
                        continue

                    try:
                        st = sensor.get_status(self.reactor.monotonic())
                        val = 1 if st.get('filament_detected', False) else 0
                    except Exception as e:
                        logging.info(f"[PregateAutoLoad] Sensor read error lane {lane}: {e}")
                        val = self.last_state[lane]  # не меняем состояние при ошибке

                    if val != self.last_state[lane]:
                        logging.info(f"[PregateAutoLoad] Lane {lane} changed {self.last_state[lane]} -> {val}")
                        # Rising edge 0 -> 1 — запускаем автозагрузку
                        if self.last_state[lane] == 0 and val == 1:
                            # планируем обработку в основной поток (через очередь)
                            self._enqueue_filament_detected(lane)
                        self.last_state[lane] = val

                time.sleep(self.poll_interval)

            except Exception as e:
                logging.info(f"[PregateAutoLoad] Ошибка в цикле: {e}")
                time.sleep(1.0)

    def _enqueue_filament_detected(self, lane):
        if not self.mmu_ready:
            logging.info(f"[PregateAutoLoad] Ignored lane {lane} — MMU not ready yet.")
            return

        """
        Добавляем команду в очередь и запускаем процессор очереди если он не запущен.
        """
        try:
            # Проверим gate sensor (имя секции — подставь своё, здесь sp_sensor_runout используется по умолчанию)
            try:
                gate_sensor = self.printer.lookup_object("filament_switch_sensor sp_sensor_runout")
                gate_status = gate_sensor.get_status(self.reactor.monotonic())
                gate_present = 1 if gate_status.get('filament_detected', False) else 0
            except Exception:
                gate_present = 0

            # Формируем команду: SP_LOAD_HUB LANE=<mode>,<lane>
            # mode: 0 = gate free (load to gate then park), 1 = gate occupied (just pull a bit)
            mode = 1 if gate_present else 0
            cmd = f"SP_LOAD_HUB NO_SENSOR_CHECK={mode} LANE={lane}"

            logging.info(f"[PregateAutoLoad] Queuing command for lane {lane}: {cmd}")
            self.cmd_queue.append(cmd)

            # зарегистрируем процессор очереди, если он ещё не зарегистрирован
            if not self.queue_processor_registered:
                # register_callback вызывает обработчик вскоре в reactor'е
                self.reactor.register_callback(self._process_queue_callback)
                self.queue_processor_registered = True

        except Exception as e:
            logging.info(f"[PregateAutoLoad] Ошибка при постановке в очередь для LANE {lane}: {e}")

    def _process_queue_callback(self, eventtime):
        """
        Callback для reactor'а — обрабатывает очередь команд.
        Должен возвращать либо waketime (eventtime + N) для повтора, либо None если завершил.
        """
        try:
            # если очередь пуста — снимаем флаг и завершаем (не перерегистрируемся)
            if not self.cmd_queue:
                self.queue_processor_registered = False
                return None

            # проверим состояние принтера — можно выполнять только в idle/ready/standby
            try:
                ps = self.printer.lookup_object("print_stats")
                state = ps.get_status(self.reactor.monotonic()).get('state', '').lower()
            except Exception:
                sm = self.printer.get_state_message()
                if isinstance(sm, (list, tuple)) and len(sm) > 0:
                    state = str(sm[0]).lower()
                else:
                    state = str(sm).lower()

            allowed_states = ("idle", "ready", "standby")
            if state not in allowed_states:
                # отложим повтор на 1 секунду
                return eventtime + 1.0

            # берём первую команду из очереди и выполняем её
            cmd = self.cmd_queue.pop(0)
            try:
                logging.info(f"[PregateAutoLoad] Executing queued command: {cmd}")
                # run_script_from_command может бросить исключение — ловим
                self.gcode.run_script_from_command(cmd)
                logging.info(f"[PregateAutoLoad] Executed: {cmd}")
            except Exception as e:
                logging.info(f"[PregateAutoLoad] Ошибка выполнения команды '{cmd}': {e}")
                # Не перебрасываем — просто записали ошибку и продолжаем

            # Если в очереди ещё есть команды — вернём waketime чтобы мы вызвались снова почти сразу
            if self.cmd_queue:
                return eventtime + 0.1
            else:
                self.queue_processor_registered = False
                return None

        except Exception as e:
            logging.info(f"[PregateAutoLoad] Exception in queue processor: {e}")
            # В случае серьёзной ошибки — попробовать снова через секунду
            return eventtime + 1.0


def load_config(config):
    return PregateAutoLoad(config)
