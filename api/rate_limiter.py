"""
API 限流器 - Token Bucket 實作
"""
import time
from threading import Lock
from typing import Optional

from loguru import logger


class RateLimiter:
    """
    Token Bucket 限流器

    用於控制 API 呼叫頻率，避免超過 FinMind 限制。
    - 免費用戶: 300 次/小時
    - 註冊用戶: 600 次/小時
    """

    def __init__(self, calls_per_hour: int = 600):
        """
        初始化限流器

        Args:
            calls_per_hour: 每小時允許的呼叫次數
        """
        self.calls_per_hour = calls_per_hour
        self.min_interval = 3600.0 / calls_per_hour  # 每次呼叫最小間隔（秒）
        self.last_call_time: float = 0.0
        self._lock = Lock()
        self._call_count = 0
        self._hour_start = time.time()

        logger.info(
            f"RateLimiter 初始化: {calls_per_hour} 次/小時, "
            f"最小間隔 {self.min_interval:.2f} 秒"
        )

    def wait(self) -> float:
        """
        等待直到可以發送下一個請求

        Returns:
            實際等待的秒數
        """
        with self._lock:
            now = time.time()

            # 重置小時計數器
            if now - self._hour_start >= 3600:
                self._hour_start = now
                self._call_count = 0
                logger.debug("限流計數器已重置")

            # 計算需要等待的時間
            elapsed = now - self.last_call_time
            wait_time = 0.0

            if elapsed < self.min_interval:
                wait_time = self.min_interval - elapsed
                logger.debug(f"等待 {wait_time:.2f} 秒...")
                time.sleep(wait_time)

            self.last_call_time = time.time()
            self._call_count += 1

            return wait_time

    def get_stats(self) -> dict:
        """取得限流統計資訊"""
        with self._lock:
            now = time.time()
            hour_elapsed = now - self._hour_start
            remaining_calls = self.calls_per_hour - self._call_count

            return {
                "calls_this_hour": self._call_count,
                "remaining_calls": max(0, remaining_calls),
                "hour_elapsed_seconds": hour_elapsed,
                "next_reset_in": max(0, 3600 - hour_elapsed),
            }

    def __enter__(self):
        """Context manager 進入"""
        self.wait()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager 離開"""
        pass


class RetryHandler:
    """
    重試處理器

    根據 SPEC 規格:
    - 4XX（除 429）: 不重試，記錄錯誤
    - 5XX / 429: 重試 3 次（5分/10分/1小時）
    """

    DEFAULT_RETRY_INTERVALS = [300, 600, 3600]  # 秒

    def __init__(
        self,
        max_retries: int = 3,
        retry_intervals: Optional[list[int]] = None
    ):
        """
        初始化重試處理器

        Args:
            max_retries: 最大重試次數
            retry_intervals: 每次重試的等待間隔（秒）
        """
        self.max_retries = max_retries
        self.retry_intervals = retry_intervals or self.DEFAULT_RETRY_INTERVALS

        # 確保間隔列表長度足夠
        while len(self.retry_intervals) < max_retries:
            self.retry_intervals.append(self.retry_intervals[-1])

    def should_retry(self, status_code: int, retry_count: int) -> bool:
        """
        判斷是否應該重試

        Args:
            status_code: HTTP 狀態碼
            retry_count: 當前重試次數

        Returns:
            是否應該重試
        """
        if retry_count >= self.max_retries:
            return False

        # 5XX 或 429 才重試
        return status_code >= 500 or status_code == 429

    def get_wait_time(self, retry_count: int) -> int:
        """
        取得下次重試的等待時間

        Args:
            retry_count: 當前重試次數（從0開始）

        Returns:
            等待秒數
        """
        if retry_count < len(self.retry_intervals):
            return self.retry_intervals[retry_count]
        return self.retry_intervals[-1]

    def wait_for_retry(self, retry_count: int) -> int:
        """
        等待後重試

        Args:
            retry_count: 當前重試次數

        Returns:
            等待的秒數
        """
        wait_time = self.get_wait_time(retry_count)
        logger.warning(
            f"重試 {retry_count + 1}/{self.max_retries}, "
            f"等待 {wait_time} 秒..."
        )
        time.sleep(wait_time)
        return wait_time
