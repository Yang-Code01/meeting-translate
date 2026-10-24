"""模型统计：各模型平均翻译延迟、历史会议的 AIC 消耗，用于在设置里提示速度与成本。

存于 %APPDATA%\\meeting-translate\\model_stats.json。
"""

from __future__ import annotations

import json
import logging
import threading

from .config import STATS_FILE

log = logging.getLogger(__name__)

# 本机实测的稳态延迟（秒），没有自测数据时作初值
BUILTIN_LATENCY = {
    "gpt-6-luna": 1.6,
    "gpt-5.4-mini": 1.2,
    "claude-haiku-4.5": 2.5,
    "claude-haiku-5.5": 2.5,
    "gpt-5.6-luna": 1.6,
}
# 无历史时：每小时 600 句 × (300 输入 + 30 输出 token)；AIC/h = K × 混合单价
DEFAULT_K = 600 * 330 / 1e6
MIN_MEETING_HOURS = 3 / 60


def blended_price(input_price: float, output_price: float) -> float:
    """按输入:输出 = 10:1 的 token 比例混合的单价（AIC/百万 token）。"""
    return (10 * input_price + output_price) / 11


class ModelStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict = {"latency": {}, "meetings": []}
        try:
            if STATS_FILE.exists():
                self._data.update(json.loads(STATS_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            log.exception("读取模型统计失败")

    def _save(self) -> None:
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATS_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            log.exception("保存模型统计失败")

    def record_latency(self, model: str, seconds: float) -> None:
        with self._lock:
            d = self._data["latency"].setdefault(model, {"sum": 0.0, "n": 0})
            d["sum"] += seconds
            d["n"] += 1
            self._save()

    def latency(self, model: str) -> tuple[float | None, bool]:
        """(平均延迟, 是否为本机实测)。"""
        with self._lock:
            d = self._data["latency"].get(model)
            if d and d["n"]:
                return d["sum"] / d["n"], True
        return BUILTIN_LATENCY.get(model), False

    def record_meeting(self, aic: float, hours: float, price: float) -> None:
        if hours < MIN_MEETING_HOURS or aic <= 0 or price <= 0:
            return
        with self._lock:
            self._data["meetings"].append({"aic": aic, "hours": hours, "price": price})
            self._data["meetings"] = self._data["meetings"][-50:]
            self._save()

    def k(self) -> float:
        with self._lock:
            ms = self._data["meetings"]
            denom = sum(m["hours"] * m["price"] for m in ms)
            return sum(m["aic"] for m in ms) / denom if denom > 0 else DEFAULT_K

    def aic_per_hour(self, price: float) -> float:
        return self.k() * price


_stats: ModelStats | None = None


def get() -> ModelStats:
    global _stats
    if _stats is None:
        _stats = ModelStats()
    return _stats
