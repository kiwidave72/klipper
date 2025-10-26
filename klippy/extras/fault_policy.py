# Add a fault policy to capture MCU faults, sandboxing MCU and its configuration
#
# Copyright (C) 2025  David Norden <kiwidave72@hotmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from typing import Callable, Dict

class FaultPolicyBase:
    """Interface for handling safety faults."""
    name = "printer"  # identifier key
    def __init__(self, printer):
        self.printer = printer
    def trip(self, *, code: str, msg: str, heater=None, mcu=None):
        raise NotImplementedError

# ----- Built-in policies ------------------------------------------------

class PrinterPolicy(FaultPolicyBase):
    """Stock Klipper behavior: full printer shutdown."""
    name = "printer"
    def trip(self, *, code, msg, heater=None, mcu=None):
        self.printer.invoke_shutdown(f"{code}: {msg}")

class SandboxLatchPolicy(FaultPolicyBase):
    """
    Latch the offending heater OFF; do NOT shut down the whole printer.
    Intended for sandbox/test MCUs. No power cut / no reset.
    """
    name = "sandbox_latch"
    def trip(self, *, code, msg, heater=None, mcu=None):
        try:
            if heater is not None:
                if hasattr(heater, "set_temp"):  heater.set_temp(0.0)
                if hasattr(heater, "set_power"): heater.set_power(0.0)
        except Exception:
            pass
        wh = self.printer.lookup_object("webhooks", None)
        if wh:
            wh.add_event("fault", {"scope":"sandbox", "policy":self.name,
                                   "code":code, "msg":msg,
                                   "heater":getattr(heater, "name", None),
                                   "mcu":getattr(mcu, "name", None)})
        gcode = self.printer.lookup_object("gcode")
        raise gcode.error(f"[{self.name}] {code}: {msg}")

# ----- Registry & factory ----------------------------------------------

_REGISTRY: Dict[str, Callable] = {
    PrinterPolicy.name:      PrinterPolicy,
    SandboxLatchPolicy.name: SandboxLatchPolicy,
}

def register_policy(cls: type):
    _REGISTRY[cls.name] = cls
    return cls

def make_policy(printer, name: str) -> FaultPolicyBase:
    ctor = _REGISTRY.get(name) or _REGISTRY["printer"]
    return ctor(printer)

