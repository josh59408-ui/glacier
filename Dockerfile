# 台股技術分析篩選系統 Dockerfile
FROM python:3.11-slim

# 設定工作目錄
WORKDIR /app

# 安裝系統依賴
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 複製依賴檔案
COPY requirements.txt .

# 安裝 Python 依賴
RUN pip install --no-cache-dir -r requirements.txt

# 複製專案程式碼
COPY . .

# 建立日誌目錄
RUN mkdir -p /app/logs

# 設定環境變數
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# 預設執行指令
CMD ["python", "main.py", "health"]
