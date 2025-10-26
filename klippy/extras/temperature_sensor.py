# Support generic temperature sensors
#
# Copyright (C) 2019  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

KELVIN_TO_CELSIUS = -273.15

# NEW: route faults via the named policy (printer|sandbox_latch|...)
from fault_policy import make_policy

class PrinterSensorGeneric:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        pheaters = self.printer.load_object(config, 'heaters')
        self.sensor = pheaters.setup_sensor(config)
        self.min_temp = config.getfloat('min_temp', KELVIN_TO_CELSIUS,
                                        minval=KELVIN_TO_CELSIUS)
        self.max_temp = config.getfloat('max_temp', 99999999.9,
                                        above=self.min_temp)
        self.sensor.setup_minmax(self.min_temp, self.max_temp)
        self.sensor.setup_callback(self.temperature_callback)
        pheaters.register_sensor(config, self)
        self.last_temp = 0.
        self.measured_min = 99999999.
        self.measured_max = 0.

    def _resolve_mcu_for_policy(self):
        """Best-effort attempt to find the MCU object associated with this sensor."""
        # Different sensor backends expose different handles; try common ones.
        try:
            # Many ADC-based sensors have .mcu_adc.get_mcu()
            mcu_adc = getattr(self.sensor, 'mcu_adc', None)
            if mcu_adc and hasattr(mcu_adc, 'get_mcu'):
                return mcu_adc.get_mcu()
            # Some might expose .mcu or a direct .get_mcu()
            mcu = getattr(self.sensor, 'mcu', None)
            if mcu is not None:
                return mcu
            if hasattr(self.sensor, 'get_mcu'):
                return self.sensor.get_mcu()
        except Exception:
            pass
        return None

    def temperature_callback(self, read_time, temp):
        # Record readings as before
        self.last_temp = temp
        if temp:
            self.measured_min = min(self.measured_min, temp)
            self.measured_max = max(self.measured_max, temp)

        # NEW: Policy-based guard for out-of-range readings.
        # (Many backends enforce min/max themselves; this adds a consistent policy path.)
        if temp < self.min_temp or temp > self.max_temp:
            mcu = self._resolve_mcu_for_policy()
            pname = getattr(mcu, 'get_fault_policy_name', lambda: 'printer')()
            make_policy(self.printer, pname).trip(
                code="SENSOR_OUT_OF_RANGE",
                msg=("Temperature sensor '%s' reading %.2f outside range "
                     "(%.2f..%.2f)") % (self.name, temp, self.min_temp, self.max_temp),
                heater=None,
                mcu=mcu
            )

    def get_temp(self, eventtime):
        return self.last_temp, 0.

    def stats(self, eventtime):
        return False, '%s: temp=%.1f' % (self.name, self.last_temp)

    def get_status(self, eventtime):
        return {
            'temperature': round(self.last_temp, 2),
            'measured_min_temp': round(self.measured_min, 2),
            'measured_max_temp': round(self.measured_max, 2)
        }

def load_config_prefix(config):
    return PrinterSensorGeneric(config)
