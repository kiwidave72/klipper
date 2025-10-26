# Helper for aggregating and exposing MCU-related error context
#
# Copyright (C) 2016-2025  Kevin O'Connor
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import traceback

class PrinterMCUError:
    """
    Lightweight helper that centralizes MCU error context for the UI
    and other modules. It is intentionally minimal and safe to load
    even if nothing ever calls into it.

    Features:
      - Stores recent MCU error messages and reasons
      - Optional `add_clarify(text)` API for extra context (used by sandbox/policy)
      - Exposes `get_status()` so frontends can render extra info
    """

    MAX_HISTORY = 20

    def __init__(self, config):
        self.printer = config.get_printer()
        self._history = []          # list of dicts: {mcu, event_type, reason, extra, clock}
        self._clarify_notes = []    # free-form strings added by other modules
        self._last = None           # last error dict (or None)

        # Hook into lifecycle to enrich history when available
        self.printer.register_event_handler("klippy:analyze_shutdown",
                                            self._on_analyze_shutdown)
        self.printer.register_event_handler("klippy:shutdown",
                                            self._on_shutdown)

        # Also keep a very small breadcrumb that we successfully loaded
        logging.info("error_mcu: initialized")

    # -------- Public convenience API --------

    def add_clarify(self, text):
        """Optional API used by sandbox/fault policy to enrich error context."""
        try:
            msg = str(text)
        except Exception:
            msg = repr(text)
        self._clarify_notes.append(msg)
        if len(self._clarify_notes) > self.MAX_HISTORY:
            self._clarify_notes = self._clarify_notes[-self.MAX_HISTORY:]
        logging.info("MCU error clarify: %s", msg)

    # -------- Printer event handlers --------

    def _on_analyze_shutdown(self, message, details):
        """
        Called by core when transitioning into shutdown, with raw MCU
        clocksync/serial debug text available. We only store light
        context here to avoid huge memory churn.
        """
        try:
            entry = {
                "src": "analyze_shutdown",
                "message": str(message),
                "mcu": (details or {}).get("mcu") if isinstance(details, dict) else None,
                "event_type": (details or {}).get("event_type") if isinstance(details, dict) else None,
                "reason": (details or {}).get("reason") if isinstance(details, dict) else None,
                "shutdown_clock": (details or {}).get("shutdown_clock") if isinstance(details, dict) else None,
            }
            self._append_history(entry)
        except Exception:
            logging.exception("error_mcu: exception in _on_analyze_shutdown")

    def _on_shutdown(self, force=False):
        # Nothing special; this fires for any shutdown. We keep it to
        # ensure `error_mcu` remains active in the event chain.
        pass

    # -------- Internal helpers --------

    def _append_history(self, entry: dict):
        self._last = entry
        self._history.append(entry)
        if len(self._history) > self.MAX_HISTORY:
            self._history = self._history[-self.MAX_HISTORY:]

    # -------- Klipper-standard query surfaces --------

    def get_status(self, eventtime=None):
        """Expose recent MCU error context to the UI."""
        last = self._last or {}
        return {
            "last_event": {
                "mcu": last.get("mcu"),
                "event_type": last.get("event_type"),
                "reason": last.get("reason"),
                "message": last.get("message"),
                "shutdown_clock": last.get("shutdown_clock"),
            },
            "clarify": list(self._clarify_notes),  # copy
            "history_len": len(self._history),
        }

    def stats(self, eventtime):
        """Emit a single-line log-friendly string (optional)."""
        last = self._last or {}
        msg = ("error_mcu: last_event mcu=%s type=%s reason=%s"
               % (last.get("mcu"), last.get("event_type"), last.get("reason")))
        # Returning (is_active, line)
        return False, msg


def load_config(config):
    """Standard Klipper module entrypoint."""
    return PrinterMCUError(config)
