# Virtual Pins support
#
# Original (C) 2023 Pedro Lamas <pedrolamas@gmail.com>
# Updated: per-pin defaults via [virtual_pins <name>], ADC smoothing/clamping,
#          runtime tuning, robust loader hooks that CONSUME prefix options.
#
# License: GNU GPLv3

class VirtualPins:
    def __init__(self, config):
        self._printer = config.get_printer()
        # Discoverable handle
        self._printer.add_object('virtual_pins', self)

        # Register the pin "chip" so "virtual_pin:<name>" resolves
        ppins = self._printer.lookup_object('pins')
        ppins.register_chip('virtual_pin', self)

        self._pins = {}
        self._oid_count = 0
        self._config_callbacks = []

        # Per-pin options captured from [virtual_pins <name>] prefix sections
        # e.g. self._prefix_opts['test_temp'] = {'default': 0.55, 'type': 'adc'}
        self._prefix_opts = {}

        self._printer.register_event_handler("klippy:connect",
                                             self.handle_connect)

    # Called from load_config_prefix to store consumed options
    def set_prefix_options(self, pin_name, default_val, ptype):
        self._prefix_opts[pin_name] = {}
        if default_val is not None:
            self._prefix_opts[pin_name]['default'] = default_val
        if ptype is not None:
            self._prefix_opts[pin_name]['type'] = ptype

    def handle_connect(self):
        for cb in self._config_callbacks:
            cb()

    # Read extra options from our own stash or from config (fallback)
    def _load_pin_config(self, name):
        # name may be "virtual_pin:<pinname>" or "<pinname>"
        pin_name = name.split(':', 1)[1] if ':' in name else name

        # 1) Prefer values captured by load_config_prefix (already consumed)
        if pin_name in self._prefix_opts:
            return dict(self._prefix_opts[pin_name])

        # 2) Fallback: try reading the section directly (in case prefix loader missed)
        #    This won't mark options as used (validator may complain), so prefix path is preferred.
        cfg = self._printer.lookup_object('configfile')
        sect_name = f"virtual_pins {pin_name}"  # plural
        try:
            sect = cfg.getsection(sect_name)
        except Exception:
            return {}
        out = {}
        try:
            out['default'] = sect.getfloat('default', None)
        except Exception:
            pass
        try:
            out['type'] = sect.get('type', None)
        except Exception:
            pass
        return out

    # Called by Klipper's pin system to materialize a pin object
    def setup_pin(self, pin_type, pin_params):
        ppins = self._printer.lookup_object('pins')
        name = pin_params['pin']
        if name in self._pins:
            return self._pins[name]

        # Merge per-pin config (default value, explicit type, etc.)
        extra = self._load_pin_config(name)
        merged = dict(pin_params)
        for k, v in extra.items():
            if v is not None:
                merged[k] = v

        # Allow explicit override via captured type; otherwise use caller's pin_type
        ptype = merged.get('type', pin_type)

        if ptype == 'digital_out':
            pin = DigitalOutVirtualPin(self, merged)
        elif ptype == 'pwm':
            pin = PwmVirtualPin(self, merged)
        elif ptype == 'adc':
            pin = AdcVirtualPin(self, merged)
        elif ptype == 'endstop':
            pin = EndstopVirtualPin(self, merged)
        else:
            raise ppins.error("unable to create virtual pin of type %s" % (ptype,))

        self._pins[name] = pin
        return pin

    # --- Minimal MCU plumbing for Klipper interfaces ---
    def create_oid(self):
        self._oid_count += 1
        return self._oid_count - 1

    def register_config_callback(self, cb):
        self._config_callbacks.append(cb)

    def add_config_cmd(self, cmd, is_init=False, on_restart=False):
        pass

    def get_query_slot(self, oid):
        return 0

    def seconds_to_clock(self, time):
        return 0

    def get_printer(self):
        return self._printer

    def register_response(self, cb, msg, oid=None):
        pass

    def alloc_command_queue(self):
        pass

    def lookup_command(self, msgformat, cq=None):
        return VirtualCommand()

    def lookup_query_command(self, msgformat, respformat, oid=None,
                             cq=None, is_async=False):
        return VirtualCommandQuery(respformat, oid)

    def get_enumerations(self):
        return {}

    def print_time_to_clock(self, print_time):
        return 0

    def estimated_print_time(self, eventtime):
        return 0

    def register_stepqueue(self, stepqueue):
        pass

    def request_move_queue_slot(self):
        pass

    def get_status(self, eventtime):
        return {
            'pins': {
                name: pin.get_status(eventtime)
                for name, pin in self._pins.items()
            }
        }


class VirtualCommand:
    def send(self, data=(), minclock=0, reqclock=0):
        pass

    def get_command_tag(self):
        pass


class VirtualCommandQuery:
    def __init__(self, respformat, oid):
        entries = respformat.split()
        self._response = {}
        for entry in entries[1:]:
            key, _ = entry.split('=')
            self._response[key] = oid if key == 'oid' else 1

    def send(self, data=(), minclock=0, reqclock=0):
        return self._response

    def send_with_preface(self, preface_cmd, preface_data=(), data=(),
                          minclock=0, reqclock=0):
        return self._response


class VirtualPin:
    def __init__(self, mcu, pin_params):
        self._mcu = mcu
        self._name = pin_params['pin']
        self._pullup = pin_params.get('pullup', False)
        self._invert = pin_params.get('invert', False)
        self._value = self._pullup

        printer = self._mcu.get_printer()
        self._real_mcu = printer.lookup_object('mcu')
        gcode = printer.lookup_object('gcode')

        # Per-pin mux command:
        #   SET_VIRTUAL_PIN PIN=virtual_pin:<name> VALUE=<0..1>
        gcode.register_mux_command("SET_VIRTUAL_PIN", "PIN", self._name,
                                   self.cmd_SET_VIRTUAL_PIN,
                                   desc=self.cmd_SET_VIRTUAL_PIN_help)

    cmd_SET_VIRTUAL_PIN_help = "Set the value of a virtual pin (0..1)"
    def cmd_SET_VIRTUAL_PIN(self, gcmd):
        self._value = gcmd.get_float('VALUE', minval=0., maxval=1.)

    def get_mcu(self):
        return self._real_mcu


class DigitalOutVirtualPin(VirtualPin):
    def __init__(self, mcu, pin_params):
        super().__init__(mcu, pin_params)
        # default from [virtual_pins <name>] or pullup state
        self._value = float(pin_params.get('default',
                                           1.0 if self._pullup else 0.0))

    def setup_max_duration(self, max_duration):
        pass

    def setup_start_value(self, start_value, shutdown_value):
        self._value = start_value

    def set_digital(self, print_time, value):
        self._value = value

    def get_status(self, eventtime):
        return {'value': self._value, 'type': 'digital_out'}


class PwmVirtualPin(VirtualPin):
    def __init__(self, mcu, pin_params):
        super().__init__(mcu, pin_params)
        self._value = float(pin_params.get('default', 0.0))

    def setup_max_duration(self, max_duration):
        pass

    def setup_start_value(self, start_value, shutdown_value):
        self._value = start_value

    def setup_cycle_time(self, cycle_time, hardware_pwm=False):
        pass

    def set_pwm(self, print_time, value, cycle_time=None):
        self._value = value

    def get_status(self, eventtime):
        return {'value': self._value, 'type': 'pwm'}


class AdcVirtualPin(VirtualPin):
    """
    Virtual ADC pin:
      - default start value from [virtual_pins <name>].default (0..1)
      - exponential smoothing
      - clamped mapping to avoid SENSOR_OUT_OF_RANGE
      - rate-limited emissions to Klipper's ADC callback
      - runtime tuning via: VADC_TUNE PIN=virtual_pin:<name> ALPHA=.. DELTA=.. PERIOD=..
    """
    def __init__(self, mcu, pin_params):
        super().__init__(mcu, pin_params)

        d = float(pin_params.get('default', 0.5))  # 0..1 ratio
        self._value = d
        self._filtered = d

        # Sample scaling (Klipper expects 0..Vref-ish units)
        self._min_sample = 0.0
        self._max_sample = 1.0

        # Callback cadence
        self._report_time = 2.0

        # Smoothing/thresholds
        self._smoothing_alpha = 0.2     # smaller = smoother
        self._min_emit_delta = 0.01     # min change in *sample* units
        self._min_emit_period = 1.0     # seconds

        self._callback = None
        self._last_emit_value = None
        self._last_emit_time = 0.0

        printer = self._mcu.get_printer()
        gcode = printer.lookup_object('gcode')
        gcode.register_mux_command("VADC_TUNE", "PIN", self._name,
                                   self.cmd_VADC_TUNE,
                                   desc="Tune virtual ADC smoothing and thresholds")
        printer.register_event_handler("klippy:connect", self.handle_connect)

    # Console helper:
    #   VADC_TUNE PIN=virtual_pin:test_temp ALPHA=0.1 DELTA=0.02 PERIOD=0.5
    def cmd_VADC_TUNE(self, gcmd):
        if gcmd.get('ALPHA', None) is not None:
            self._smoothing_alpha = max(0.0, min(1.0, float(gcmd.get('ALPHA'))))
        if gcmd.get('DELTA', None) is not None:
            self._min_emit_delta = max(0.0, float(gcmd.get('DELTA')))
        if gcmd.get('PERIOD', None) is not None:
            self._min_emit_period = max(0.01, float(gcmd.get('PERIOD')))
        gcmd.respond_info(
            f"VADC tuned: alpha={self._smoothing_alpha}, "
            f"delta={self._min_emit_delta}, period={self._min_emit_period}s")

    def handle_connect(self):
        reactor = self._mcu.get_printer().get_reactor()
        now = reactor.monotonic()
        self._last_emit_time = now
        self._last_emit_value = self._map_and_clamp(self._filtered)
        reactor.register_timer(self._tick, now + self._report_time)

    def setup_adc_callback(self, report_time, callback):
        self._report_time = max(0.05, float(report_time))
        self._callback = callback

    def setup_adc_sample(self, sample_time, sample_count,
                         minval=0., maxval=1., range_check_count=0):
        self._min_sample = float(minval)
        self._max_sample = float(maxval)
        # Auto-tune smoothing from sample_count ~ EMA alpha ~= 2/(N+1)
        try:
            n = max(1, int(sample_count))
            self._smoothing_alpha = max(0.02, min(1.0, 2.0 / (n + 1.0)))
        except Exception:
            pass

    def _tick(self, eventtime):
        # Smooth toward commanded value
        a = self._smoothing_alpha
        self._filtered = (1.0 - a) * self._filtered + a * self._value

        sample_value = self._map_and_clamp(self._filtered)

        if self._callback:
            emit = False
            if self._last_emit_value is None:
                emit = True
            else:
                dv = abs(sample_value - self._last_emit_value)
                if dv >= self._min_emit_delta:
                    emit = True
                elif (eventtime - self._last_emit_time) >= self._min_emit_period:
                    emit = True

            if emit:
                self._callback(eventtime, sample_value)
                self._last_emit_value = sample_value
                self._last_emit_time = eventtime

        return eventtime + self._report_time

    def _map_and_clamp(self, ratio):
        # ratio 0..1 -> sample range [min,max], clamp slightly inside
        rng = self._max_sample - self._min_sample
        val = (ratio * rng) + self._min_sample
        eps = max(1e-6, 1e-4 * rng)
        if rng > 0:
            val = max(self._min_sample + eps, min(self._max_sample - eps, val))
        return val

    def get_status(self, eventtime):
        return {'value': self._filtered, 'type': 'adc'}


class EndstopVirtualPin(VirtualPin):
    def __init__(self, mcu, pin_params):
        super().__init__(mcu, pin_params)
        self._steppers = []
        # default from [virtual_pins <name>] or pullup state
        self._value = float(pin_params.get('default',
                                           1.0 if self._pullup else 0.0))

    def add_stepper(self, stepper):
        self._steppers.append(stepper)

    def query_endstop(self, print_time):
        return self._value

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        reactor = self._mcu.get_printer().get_reactor()
        completion = reactor.completion()
        completion.complete(True)
        return completion

    def home_wait(self, home_end_time):
        return 1

    def get_steppers(self):
        return list(self._steppers)

    def get_status(self, eventtime):
        return {'value': self._value, 'type': 'endstop'}


# --- Loader hooks ---
def load_config(config):
    """
    Invoked when a [virtual_pins] section exists.
    Ensures the chip is registered before any 'virtual_pin:*' references.
    """
    return VirtualPins(config)

def load_config_prefix(config):
    """
    Invoked for each [virtual_pins <name>] section.
    CONSUMES options (default/type) so the validator is happy,
    and stores them into the module for later use.
    """
    # Determine pin name after the prefix
    full_name = config.get_name()            # e.g. "virtual_pins test_temp"
    pin_name = full_name.split(' ', 1)[1] if ' ' in full_name else ''

    # Consume options (marks them as used)
    default_val = None
    ptype = None
    try:
        default_val = config.getfloat('default')
    except Exception:
        pass
    try:
        ptype = config.get('type')
    except Exception:
        pass

    # Ensure the module exists and stash the options
    printer = config.get_printer()
    vp = printer.lookup_object('virtual_pins', None)
    if vp is None:
        vp = VirtualPins(config)
    if pin_name:
        vp.set_prefix_options(pin_name, default_val, ptype)
    return vp
