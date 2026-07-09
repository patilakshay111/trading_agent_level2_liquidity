from __future__ import annotations

import ctypes
import os


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwakeGuard:
    def __init__(self) -> None:
        self.active = False
        self.last_reason = "Not enabled."

    def enable(self) -> tuple[bool, str]:
        if os.name != "nt":
            self.active = False
            self.last_reason = "Keep-awake is only needed on Windows in this app."
            return True, self.last_reason
        result = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)  # type: ignore[attr-defined]
        if result == 0:
            self.active = False
            self.last_reason = "Windows rejected SetThreadExecutionState."
            return False, self.last_reason
        self.active = True
        self.last_reason = "Windows system sleep blocked while engine is running."
        return True, self.last_reason

    def disable(self) -> tuple[bool, str]:
        if os.name != "nt":
            self.active = False
            self.last_reason = "Keep-awake released."
            return True, self.last_reason
        result = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # type: ignore[attr-defined]
        if result == 0:
            self.last_reason = "Windows rejected keep-awake release."
            return False, self.last_reason
        self.active = False
        self.last_reason = "Windows keep-awake released."
        return True, self.last_reason
