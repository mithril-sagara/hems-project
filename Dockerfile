FROM python:3.11-slim

# タイムゾーン(JST)の設定
RUN apt-get update && apt-get install -y tzdata && \
    ln -sf /usr/share/zoneinfo/Asia/Tokyo /etc/localtime && \
    echo "Asia/Tokyo" > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

# 作業ディレクトリの設定
WORKDIR /app

# 依存関係のコピーとインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- ここを追加 ---
# プログラム本体と作成した証明書(cert.pem, key.pem)をすべてコピー
COPY . .

# 実行コマンド (main.pyの中でssl_contextを指定している前提)
CMD ["python", "main.py"]
