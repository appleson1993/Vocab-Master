# 使用 Python 3.11 Slim 版本作為基底映像檔
FROM python:3.11-slim

# 設定工作目錄
WORKDIR /app

# 複製 requirements.txt 並安裝依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製所有程式碼到工作目錄
COPY . .

# 建立資料庫檔案的目錄 (如果需要持久化，這一步其實是為了確保權限等)
# 在這個簡單範例中，我們直接複製了代碼，資料庫會生成在 /app 下

# 暴露 5000 port
EXPOSE 5000

# 執行程式
CMD ["python", "main.py"]
