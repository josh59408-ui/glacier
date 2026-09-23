"""
效能監控工具
"""
import time
from functools import wraps
from typing import Callable, Any

from loguru import logger


class PerformanceMonitor:
    """
    效能監控器

    提供:
    - 函數執行時間記錄
    - 效能統計
    """

    def __init__(self):
        self.metrics: dict[str, list[float]] = {}

    def timer(self, name: str) -> Callable:
        """
        裝飾器：記錄函數執行時間

        Args:
            name: 指標名稱

        Returns:
            裝飾器函數
        """
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(*args, **kwargs) -> Any:
                start = time.time()
                try:
                    result = func(*args, **kwargs)
                    return result
                finally:
                    elapsed = time.time() - start
                    self._record(name, elapsed)
                    logger.debug(f"{name}: {elapsed:.2f}s")
            return wrapper
        return decorator

    def _record(self, name: str, elapsed: float):
        """記錄指標"""
        if name not in self.metrics:
            self.metrics[name] = []
        self.metrics[name].append(elapsed)

    def get_stats(self, name: str) -> dict:
        """取得指定指標的統計"""
        if name not in self.metrics:
            return {}

        values = self.metrics[name]
        return {
            "count": len(values),
            "total": sum(values),
            "avg": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }

    def get_all_stats(self) -> dict:
        """取得所有指標的統計"""
        return {name: self.get_stats(name) for name in self.metrics}

    def clear(self):
        """清除所有指標"""
        self.metrics.clear()

    def report(self) -> str:
        """產生效能報告"""
        lines = ["=== 效能報告 ==="]
        for name, values in self.metrics.items():
            stats = self.get_stats(name)
            lines.append(
                f"{name}: 次數={stats['count']}, "
                f"總時間={stats['total']:.2f}s, "
                f"平均={stats['avg']:.2f}s"
            )
        return "\n".join(lines)


# 全域效能監控器
monitor = PerformanceMonitor()
