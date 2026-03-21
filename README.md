作成したInfluxDB対応・Docker構成用のREADME.mdを作成しました。
GitHubのリポジトリにそのまま置ける構成にしています。
HEMS Dashboard (InfluxDB & Docker Edition)

太陽光発電システム（QCELLS Q.READY等）およびスマートメーターからECHONET Liteプロトコルを使用してデータを取得し、リアルタイム表示および蓄積を行うHEMSダッシュボードです。
🌟 主な特徴

    時系列DB採用: SQLiteからInfluxDB 2.7へ移行し、長期的なデータ蓄積と高速な集計に対応。

    フルDocker構成: InfluxDBとアプリを数秒で起動可能。

    高精度な表示: 1分単位のリアルタイム更新と、Open-Meteo APIによる発電予測データの統合。

    最適化されたネットワーク: network_mode: "host" により、コンテナ内から宅内ECHONET Lite機器への確実な疎通を実現。

    日本時間対応: コンテナ内部・DB・UIすべてのタイムゾーンを Asia/Tokyo に完全同期。

🛠 構成図
🚀 セットアップ手順
1. 事前準備

    Docker および Docker Compose がインストールされていること。

    ECHONET Lite対応機器（太陽光・スマートメーター）が同一ネットワーク内にあること。

2. 環境設定 (.env)

リポジトリ直下に .env ファイルを作成し、環境に合わせて修正してください。
コード スニペット

# ネットワーク・機器設定
ECHONET_IP=192.168.0.146

# InfluxDB設定
INFLUX_URL=http://influxdb:8086
INFLUX_TOKEN=<YOUR TOKEN>
INFLUX_ORG=my-home
INFLUX_BUCKET=energy_bucket

# SwitchBot設定
SB_TOKEN=<YOUR TOKEN>
SB_SECRET=<YOUR SECRET>

3. 起動

以下のコマンドでコンテナをビルド・起動します。
Bash

docker-compose up -d --build

4. アクセス

    ダッシュボード: http://<ラズパイのIP>:8000

    InfluxDB管理画面: http://<ラズパイのIP>:8086

        User: admin / Pass: password1234

        Token: my-super-secret-auth-token (docker-compose.ymlで変更可能)

📊 機能詳細

    リアルタイム監視: 発電、売電、買電、自己消費量をW単位で表示。

    自己消費率 (SCR): 発電量に対する自己消費の割合を計算。

    予測機能: 気象データに基づき、今後の発電予測値を点線グラフで表示。

    統計分析: 1時間、日、月、年単位での切り替え表示。

    蓄電池シミュレーション: 夜間買電量に基づいた推奨電池容量の算出。

📂 ファイル構成

    main.py: Flaskアプリ本体（データ収集・API・UI）

    docker-compose.yml: マルチコンテナの定義

    Dockerfile: アプリ実行環境の定義

    requirements.txt: 必要なPythonライブラリ

⚠️ 注意事項

    本システムはECHONET Liteプロトコルを使用します。機器側の設定で、LAN経由の操作が許可されていることを確認してください。

    デフォルトのパスワードやトークンは公開設定になっています。外部からアクセス可能な環境に設置する場合は、必ず docker-compose.yml 内の認証情報を変更してください。
