"""
台股交易日曆工具
"""
from datetime import date, timedelta
from typing import Optional

from loguru import logger


class TradingCalendar:
    """
    台股交易日曆

    判斷是否為交易日，並提供查找最近交易日的功能。

    台股休市日：
    - 週六、週日
    - 國定假日（需額外維護）
    """

    # 2024-2026 台股國定假日（可根據需要擴充）
    # 資料來源：台灣證券交易所
    HOLIDAYS = {
        # 2024
        date(2024, 1, 1),   # 元旦
        date(2024, 2, 8),   # 除夕
        date(2024, 2, 9),   # 春節
        date(2024, 2, 12),  # 春節
        date(2024, 2, 13),  # 春節
        date(2024, 2, 14),  # 春節
        date(2024, 2, 28),  # 和平紀念日
        date(2024, 4, 4),   # 兒童節
        date(2024, 4, 5),   # 清明節
        date(2024, 5, 1),   # 勞動節
        date(2024, 6, 10),  # 端午節
        date(2024, 9, 17),  # 中秋節
        date(2024, 10, 10), # 國慶日
        # 2025
        date(2025, 1, 1),   # 元旦
        date(2025, 1, 23),  # 春節調整休市（補假）
        date(2025, 1, 24),  # 春節調整休市（補假）
        date(2025, 1, 27),  # 除夕
        date(2025, 1, 28),  # 春節
        date(2025, 1, 29),  # 春節
        date(2025, 1, 30),  # 春節
        date(2025, 1, 31),  # 春節
        date(2025, 2, 28),  # 和平紀念日
        date(2025, 4, 3),   # 兒童節（彈性放假）
        date(2025, 4, 4),   # 兒童節/清明節
        date(2025, 5, 1),   # 勞動節
        date(2025, 5, 30),  # 端午節（彈性放假）
        date(2025, 5, 31),  # 端午節
        date(2025, 9, 29),  # 中秋節（彈性放假）
        date(2025, 10, 6),  # 中秋節
        date(2025, 10, 10), # 國慶日
        date(2025, 10, 24), # 國慶日調整休市（補假）
        date(2025, 12, 25), # 聖誕節（行憲紀念日）
        # 2026
        date(2026, 1, 1),   # 元旦
        date(2026, 1, 2),   # 元旦彈性放假
        date(2026, 2, 16),  # 除夕
        date(2026, 2, 17),  # 春節
        date(2026, 2, 18),  # 春節
        date(2026, 2, 19),  # 春節
        date(2026, 2, 20),  # 春節
        date(2026, 2, 27),  # 和平紀念日（彈性放假）
        date(2026, 2, 28),  # 和平紀念日
        date(2026, 4, 3),   # 兒童節（彈性放假）
        date(2026, 4, 4),   # 兒童節
        date(2026, 4, 5),   # 清明節
        date(2026, 4, 6),   # 清明節（彈性放假）
        date(2026, 5, 1),   # 勞動節
        date(2026, 6, 19),  # 端午節
        date(2026, 9, 25),  # 中秋節
        date(2026, 10, 9),  # 國慶日（彈性放假）
        date(2026, 10, 10), # 國慶日
    }

    # 假日清單涵蓋的最後年份
    HOLIDAYS_LAST_YEAR = 2026

    @classmethod
    def is_trading_day(cls, check_date: date) -> bool:
        """
        判斷是否為交易日

        Args:
            check_date: 要檢查的日期

        Returns:
            是否為交易日
        """
        # 檢查假日清單是否過期
        if check_date.year > cls.HOLIDAYS_LAST_YEAR:
            logger.warning(
                f"交易日曆假日清單僅涵蓋至 {cls.HOLIDAYS_LAST_YEAR} 年，"
                f"請更新 utils/trading_calendar.py 中的 HOLIDAYS"
            )

        # 週末不是交易日
        if check_date.weekday() >= 5:  # 5=週六, 6=週日
            return False

        # 國定假日不是交易日
        if check_date in cls.HOLIDAYS:
            return False

        return True

    @classmethod
    def get_previous_trading_day(
        cls,
        from_date: date,
        max_lookback: int = 10
    ) -> Optional[date]:
        """
        取得前一個交易日

        Args:
            from_date: 起始日期
            max_lookback: 最多往前查找天數

        Returns:
            前一個交易日，找不到則回傳 None
        """
        current = from_date - timedelta(days=1)

        for _ in range(max_lookback):
            if cls.is_trading_day(current):
                return current
            current -= timedelta(days=1)

        logger.warning(f"找不到 {from_date} 前 {max_lookback} 天內的交易日")
        return None

    @classmethod
    def get_next_trading_day(
        cls,
        from_date: date,
        max_lookahead: int = 10
    ) -> Optional[date]:
        """
        取得下一個交易日

        Args:
            from_date: 起始日期
            max_lookahead: 最多往後查找天數

        Returns:
            下一個交易日，找不到則回傳 None
        """
        current = from_date + timedelta(days=1)

        for _ in range(max_lookahead):
            if cls.is_trading_day(current):
                return current
            current += timedelta(days=1)

        logger.warning(f"找不到 {from_date} 後 {max_lookahead} 天內的交易日")
        return None

    @classmethod
    def get_latest_trading_day(
        cls,
        from_date: Optional[date] = None,
        max_lookback: int = 10
    ) -> date:
        """
        取得最近的交易日（含當天）

        如果 from_date 是交易日則回傳 from_date，
        否則回傳前一個交易日。

        Args:
            from_date: 起始日期（預設為今天）
            max_lookback: 最多往前查找天數

        Returns:
            最近的交易日
        """
        from_date = from_date or date.today()

        if cls.is_trading_day(from_date):
            return from_date

        previous = cls.get_previous_trading_day(from_date, max_lookback)

        if previous:
            logger.info(f"{from_date} 非交易日，使用前一個交易日: {previous}")
            return previous

        # 找不到的話，回傳原日期（讓 API 去處理）
        return from_date

    @classmethod
    def is_weekend(cls, check_date: date) -> bool:
        """判斷是否為週末"""
        return check_date.weekday() >= 5

    @classmethod
    def get_trading_days_in_range(
        cls,
        start_date: date,
        end_date: date
    ) -> list[date]:
        """
        取得日期範圍內的所有交易日

        Args:
            start_date: 開始日期
            end_date: 結束日期

        Returns:
            交易日列表
        """
        trading_days = []
        current = start_date

        while current <= end_date:
            if cls.is_trading_day(current):
                trading_days.append(current)
            current += timedelta(days=1)

        return trading_days
