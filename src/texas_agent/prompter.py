"""M4 · 荷官提词器: 屏幕大字 + macOS `say` 串行播报。

提词只走这里, 保证 dealer_prompt 事件与实际播报一一对应。
TTS 在后台线程串行消费队列, `say` 不存在或失败时静默降级为纯屏幕提示。
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading


class Prompter:
    def __init__(self, bus, tts: bool = False, voice: str | None = None):
        self.bus = bus
        self.voice = voice
        self._q: queue.Queue | None = None
        if tts and shutil.which("say"):
            self._q = queue.Queue()
            threading.Thread(target=self._tts_loop, daemon=True).start()

    def prompt(self, text: str, level: str = "normal") -> None:
        """level: normal | again(超时重提) | alert(升级告警)"""
        prefix = {"normal": "", "again": "(再次提醒) ", "alert": "⚠️ "}[level]
        self.bus.emit({"type": "dealer_prompt", "text": prefix + text,
                       "level": level, "tts": self._q is not None})
        bar = "═" * max(24, len(text) * 2 + 8)
        print(f"\n{bar}\n  📢 {prefix}{text}\n{bar}")
        if self._q is not None:
            self._q.put(prefix + text)

    def _tts_loop(self) -> None:
        while True:
            text = self._q.get()
            cmd = ["say"] + (["-v", self.voice] if self.voice else []) + [text]
            try:
                subprocess.run(cmd, check=False, timeout=15)
            except Exception:
                pass
