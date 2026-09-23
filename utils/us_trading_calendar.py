"""
美股交易日曆工具
完全獨立於台股交易日曆
"""
from datetime import date, timedelta
from typing import Optional

from loguru import logger


class USMarketCalendar:
    """
    美股交易日曆

    判斷是否為交易日，並提供查找最近交易日的功能。

    美股休市日：
    - 週六、週日
    - 美國聯邦假日

    注意：美股開盤時間為美東時間 09:30-16:00
    - 夏令時（3月-11月）：台北時間 21:30-04:00+1
    - 冬令時（11月-3月）：台北時間 22:30-05:00+1
    """

    # 2024-2026 美股聯邦假日
    # 資料來源：NYSE/NASDAQ 官方
    HOLIDAYS = {
        # 2024
        date(2024, 1, 1),    # New Year's Day
        date(2024, 1, 15),   # Martin Luther King Jr. Day
        date(2024, 2, 19),   # Presidents Day
        date(2024, 3, 29),   # Good Friday
        date(2024, 5, 27),   # Memorial Day
        date(2024, 6, 19),   # Juneteenth
        date(2024, 7, 4),    # Independence Day
        date(2024, 9, 2),    # Labor Day
        date(2024, 11, 28),  # Thanksgiving Day
        date(2024, 12, 25),  # Christmas Day

        # 2025
        date(2025, 1, 1),    # New Year's Day
        date(2025, 1, 20),   # Martin Luther King Jr. Day
        date(2025, 2, 17),   # Presidents Day
        date(2025, 4, 18),   # Good Friday
        date(2025, 5, 26),   # Memorial Day
        date(2025, 6, 19),   # Juneteenth
        date(2025, 7, 4),    # Independence Day
        date(2025, 9, 1),    # Labor Day
        date(2025, 11, 27),  # Thanksgiving Day
        date(2025, 12, 25),  # Christmas Day

        # 2026
        date(2026, 1, 1),    # New Year's Day
        date(2026, 1, 19),   # Martin Luther King Jr. Day
        date(2026, 2, 16),   # Presidents Day
        date(2026, 4, 3),    # Good Friday
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day (observed, 7/4 is Saturday)
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 26),  # Thanksgiving Day
        date(2026, 12, 25),  # Christmas Day
    }

    # 提前收盤日（13:00 EST 收盤）
    # 通常是節日前一天
    EARLY_CLOSE_DAYS = {
        # 2024
        date(2024, 7, 3),    # Day before Independence Day
        date(2024, 11, 29),  # Day after Thanksgiving
        date(2024, 12, 24),  # Christmas Eve

        # 2025
        date(2025, 7, 3),    # Day before Independence Day
        date(2025, 11, 28),  # Day after Thanksgiving
        date(2025, 12, 24),  # Christmas Eve

        # 2026
        date(2026, 7, 2),    # Day before Independence Day (observed)
        date(2026, 11, 27),  # Day after Thanksgiving
        date(2026, 12, 24),  # Christmas Eve
    }

    # 假日清單涵蓋的最後年份
    HOLIDAYS_LAST_YEAR = 2026

    @classmethod
    def is_trading_day(cls, check_date: date) -> bool:
        """
        判斷是否為美股交易日

        Args:
            check_date: 要檢查的日期

        Returns:
            是否為交易日
        """
        # 檢查假日清單是否過期
        if check_date.year > cls.HOLIDAYS_LAST_YEAR:
            logger.warning(
                f"美股交易日曆假日清單僅涵蓋至 {cls.HOLIDAYS_LAST_YEAR} 年，"
                f"請更新 utils/us_trading_calendar.py 中的 HOLIDAYS"
            )

        # 週末不是交易日
        if check_date.weekday() >= 5:  # 5=週六, 6=週日
            return False

        # 聯邦假日不是交易日
        if check_date in cls.HOLIDAYS:
            return False

        return True

    @classmethod
    def is_early_close(cls, check_date: date) -> bool:
        """
        判斷是否為提前收盤日（13:00 EST 收盤）

        Args:
            check_date: 要檢查的日期

        Returns:
            是否為提前收盤日
        """
        return check_date in cls.EARLY_CLOSE_DAYS

    @classmethod
    def get_previous_trading_day(
        cls,
        from_date: date,
        max_lookback: int = 10
    ) -> Optional[date]:
        """
        取得前一個美股交易日

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

        logger.warning(f"找不到 {from_date} 前 {max_lookback} 天內的美股交易日")
        return None

    @classmethod
    def get_latest_trading_day(
        cls,
        from_date: Optional[date] = None,
        max_lookback: int = 10
    ) -> date:
        """
        取得最近的美股交易日（含當天）

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
            logger.info(f"{from_date} 非美股交易日，使用前一個交易日: {previous}")
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
        取得日期範圍內的所有美股交易日

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

    @classmethod
    def get_next_trading_day(
        cls,
        from_date: date,
        max_forward: int = 10
    ) -> Optional[date]:
        """
        取得下一個美股交易日

        Args:
            from_date: 起始日期
            max_forward: 最多往後查找天數

        Returns:
            下一個交易日，找不到則回傳 None
        """
        current = from_date + timedelta(days=1)

        for _ in range(max_forward):
            if cls.is_trading_day(current):
                return current
            current += timedelta(days=1)

        logger.warning(f"找不到 {from_date} 後 {max_forward} 天內的美股交易日")
        return None
