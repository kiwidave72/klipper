# -*- coding: utf-8 -*-
# Sandbox / fault-policy scaffolding (host side)
#
# Goal: lightweight per-MCU "policy" registry + simple G-Code to view/set.
# This module is intentionally conservative: it does not change core shutdown
# behavior; it only discovers MCUs and records a policy string for each.
#
# It is robust against older/newer Klipper builds where:
#  - Printer.get_objects() may not exist
#  - Printer.lookup_objects() may return a list ([(name, obj), ...])
#    or a dict-like mapping ({name: obj, ...})
#
# Commands:
#   SANDBOX_STATUS
#   SANDBOX_SET MCU=<name> POLICY=<printer|sandbox>
#   SANDBOX_RECONNECT MCU=<name>
#
# Copyright (C) 2025
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

POLICY_PRINTER = "printer"
POLICY_SANDBOX = "sandbox"

VALID_POLICIES = {POLICY_PRINTER, POLICY_SANDBOX}


class Sandbox:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.policies = {}         # { mcu_name: policy }
        self._discovered = []      # [mcu_name, ...]

        logging.info("Sandbox: module loaded from %s", __file__)

        # gcode commands
        self.gcode.register_command("SANDBOX_STATUS", self._cmd_status,
                                    desc="Show sandbox MCU policies")
        self.gcode.register_command("SANDBOX_SET", self._cmd_set,
                                    desc="Set sandbox policy for an MCU")
        self.gcode.register_command("SANDBOX_RECONNECT", self._cmd_reconnect,
                                    desc="Manually reconnect a sandboxed MCU")

        # events
        self.printer.register_event_handler("klippy:connect", self._on_connect)
        self.printer.register_event_handler("klippy:ready", self._on_ready)

    # ---- util: enumerate printer objects defensively ----

    def _iter_printer_objects(self):
        """
        Yield (name, obj) for all registered objects, regardless of whether
        the build exposes get_objects() (dict) or lookup_objects() (list/iter).
        """
        # Preferred: get_objects() -> dict-like
        get_objs = getattr(self.printer, "get_objects", None)
        if callable(get_objs):
            objs = get_objs()
            try:
                # dict-like
                for k, v in objs.items():
                    yield k, v
                return
            except Exception:
                pass  # fall through and try alternative

        # Fallback: lookup_objects() seen in some builds
        lookup = getattr(self.printer, "lookup_objects", None)
        if callable(lookup):
            try:
                lst = lookup()
            except Exception:
                lst = None
            if isinstance(lst, dict):
                for k, v in lst.items():
                    yield k, v
                return
            if isinstance(lst, (list, tuple)):
                # could be [(name, obj), ...] or [obj, obj, ...]
                # try to normalize
                out = []
                for item in lst:
                    if (isinstance(item, (list, tuple)) and len(item) == 2
                            and isinstance(item[0], str)):
                        out.append((item[0], item[1]))
                    else:
                        # we don't have a name: attempt to synthesize one
                        name = getattr(item, "name", None) or getattr(
                            item, "get_name", lambda: None)()
                        if not name:
                            name = type(item).__name__
                        out.append((name, item))
                for k, v in out:
                    yield k, v
                return

        # Last resort: nothing available
        logging.warning("Sandbox: unable to enumerate printer objects; "
                        "no get_objects() or usable lookup_objects().")
        return

    def _looks_like_mcu(self, name, obj):
        """Heuristic to identify MCU objects without importing internals."""
        # Names we commonly see
        if name == "mcu":
            return True
        if name.startswith("mcu "):
            return True

        # Type check by behavior: MCUs expose these methods
        has_proto = (hasattr(obj, "lookup_command")
                     and hasattr(obj, "get_constants")
                     and hasattr(obj, "get_name"))
        if not has_proto:
            return False

        # Extra sanity: get_name should be callable and return something
        try:
            oname = obj.get_name()
        except Exception:
            return False
        return isinstance(oname, str) and bool(oname)

    def _discover_mcus(self):
        """Build list of MCU logical names and return them."""
        objs = list(self._iter_printer_objects())
        logging.info("Sandbox: lookup_objects returned %d items", len(objs))
        mcus = []
        for oname, obj in objs:
            try:
                logging.debug("Sandbox: examining object '%s' of type %s",
                              oname, type(obj).__name__)
                if not self._looks_like_mcu(oname, obj):
                    continue
                # Normalize MCU short name:
                #  - 'mcu' stays 'mcu'
                #  - 'mcu foo' -> 'foo'
                short = oname
                if oname.startswith("mcu "):
                    short = oname[4:]
                elif oname != "mcu":
                    # some builds register the object under its short name already
                    short = getattr(obj, "get_name", lambda: oname)() or oname
                logging.info("Sandbox: discovered MCU '%s'", short)
                mcus.append(short)
            except Exception:
                # Never let discovery kill connect()
                logging.exception("Sandbox: error while probing object '%s'", oname)
        if mcus:
            logging.info("Sandbox: discovered MCUs: %s", mcus)
        else:
            logging.warning("Sandbox: no MCUs discovered")
        return mcus

    def _ensure_policies(self):
        """Initialize default policies for any newly found MCUs."""
        changed = False
        for n in self._discovered:
            if n not in self.policies:
                # default all to 'printer'
                self.policies[n] = POLICY_PRINTER
                changed = True
        if changed:
            logging.info("Sandbox: policies initialized: %s", self.policies)

    # ---- events ----

    def _on_connect(self):
        try:
            self._discovered = self._discover_mcus()
            self._ensure_policies()
        except Exception:
            logging.exception("Sandbox: exception in _on_connect")

    def _on_ready(self):
        # Show a concise summary on READY
        self._print_policies_banner(prefix="Sandbox policies:")

    # ---- gcode commands ----

    def _print_policies_banner(self, prefix="Policies:"):
        lines = []
        lines.append(prefix)
        for name in sorted(self.policies.keys()):
            lines.append(" - %s: %s" % (name, self.policies[name]))
        msg = "\n".join(lines)
        self.gcode.respond_info(msg, log=False)

    def _cmd_status(self, gcmd):
        # Refresh discovery in case late-bound objects appeared
        try:
            cur = set(self._discovered)
            new = set(self._discover_mcus())
            if new - cur:
                self._discovered = sorted(new)
                self._ensure_policies()
        except Exception:
            logging.exception("Sandbox: error in SANDBOX_STATUS discovery pass")
        self._print_policies_banner(prefix="Sandbox policies:")

    def _cmd_set(self, gcmd):
        name = gcmd.get('MCU')
        policy = gcmd.get('POLICY').lower()
        if policy not in VALID_POLICIES:
            raise gcmd.error("Invalid POLICY '%s' (valid: %s)"
                             % (policy, ", ".join(sorted(VALID_POLICIES))))
        # Try to normalize the name like discovery does
        if name.startswith("mcu "):
            name = name[4:]
        if name not in self.policies:
            # Allow quickly setting even if not discovered yet, but warn
            logging.warning("Sandbox: setting policy for undiscovered MCU '%s'", name)
        self.policies[name] = policy
        logging.info("Sandbox: MCU '%s' policy set to '%s'", name, policy)
        gcmd.respond_info("MCU '%s' policy set to '%s'" % (name, policy), log=False)
        self._print_policies_banner(prefix="Sandbox policies:")

    def _cmd_reconnect(self, gcmd):
        """Manually trigger reconnection attempt for a sandboxed MCU."""
        name = gcmd.get('MCU')
        
        # Normalize MCU name
        if name.startswith("mcu "):
            name = name[4:]
        
        if name not in self.policies:
            raise gcmd.error("Unknown MCU '%s'" % (name,))
        
        if self.policies[name] != "sandbox":
            raise gcmd.error("MCU '%s' is not sandboxed (policy: %s)" 
                            % (name, self.policies[name]))
        
        # Find the MCU object
        try:
            if name == "mcu":
                mcu = self.printer.lookup_object("mcu")
            else:
                mcu = self.printer.lookup_object("mcu " + name)
        except Exception as e:
            raise gcmd.error("Failed to lookup MCU '%s': %s" % (name, str(e)))
        
        # Access the connection helper and attempt reconnection
        try:
            conn_helper = mcu._conn_helper
            if not conn_helper.can_reconnect():
                raise gcmd.error("MCU '%s' is not in a reconnectable state" % (name,))
            
            gcmd.respond_info("Attempting to reconnect MCU '%s'..." % (name,))
            success = conn_helper._attempt_reconnect()
            
            if success:
                gcmd.respond_info("MCU '%s' reconnected successfully" % (name,))
            else:
                gcmd.respond_info("Failed to reconnect MCU '%s'" % (name,))
                
        except Exception as e:
            raise gcmd.error("Reconnection failed: %s" % (str(e),))

    def get_status(self, eventtime=None):
        """Return current sandbox policies for status reporting."""
        return {'policies': dict(self.policies)}


def load_config(config):
    return Sandbox(config)