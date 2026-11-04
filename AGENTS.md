# AGENTS.md

あすけんの食事記録を定期的に取得し、Discord の Webhook に投稿・編集する Python 製の常駐アプリケーションです。

## 言語ルール

- コミットの説明、PR / Issue のタイトルと本文、コードコメント・docstring、ドキュメントは日本語で書く。日本語と英数字の間には半角スペースを入れる。
- 例外: 利用者向けの Discord 投稿文言・ログ・エラーメッセージ、あすけんの日本語データ (正規表現やラベル)、引用、技術的な識別子は元の言語のまま。
- Conventional Commits を使い、type / scope は英語、description は日本語にする。
- ワークフロー (`.github/workflows/docker.yml`) の承認依頼コメント本文とその `body-includes` の一致キーは、互いに一致している必要があるため変更しない。

## 開発コマンド

- 依存インストール: `pip install -r requirements-dev.txt` (Python 3.14 想定。Dockerfile のベースイメージは `python:3.14-slim`)
- Lint: `flake8` (設定は `.flake8`。除外は `.venv,build,dist` のみ)
- 起動: `docker compose up --build -d` (`.env` が必要。`.env.example` を参照)
- 1サイクルだけ動作確認: `RUN_ONCE=1` を付けて実行する (詳細は `README.md`)。
- テストは存在しない。

## アーキテクチャ

- `src/main.py`: 設定読み込み、サイクルのループ、変更検知、投稿・編集の制御。スケジューリングは cron ではなくこのプロセスのループで行う。
- `src/asken_client.py`: `requests` だけで asken.jp にログインし、食事記録と日次サマリーを HTML から解析する。ブラウザ自動化は使わない。
- `src/discord_client.py`: Discord Webhook の投稿 (`?wait=true`) と編集 (PATCH)。
- `src/message_builder.py`: 食事メッセージ (プレーンテキスト + 画像) と日次サマリー (embed) の組み立て。
- `src/state.py`: 投稿済みメッセージの内容ハッシュとメッセージ ID を `STATE_FILE` に永続化する。
- 設定は環境変数で受け取る。秘匿情報でない変数の既定値は Dockerfile の `ENV` に置き、`Config.from_env()` では必須として読む (コード側で二重に既定値を持たない)。

## 守るべき不変条件

- 状態スロットのキー `SUMMARY_SLOT_KEY = "advice"` は変更しない。`state.json` に保存済みのサマリーメッセージを指すキーであり、変えると編集されずに重複投稿される。
- `src/message_builder.py` の出力形式を変えたら `MESSAGE_FORMAT_VERSION` (`src/main.py`) を上げる。ハッシュが変わり、投稿済みメッセージが再描画される。
- サマリーの更新判定には、コメント本文ではなく日ごとのメニュー項目のハッシュ (`day_meals_hash`) を使う。あすけんは取得のたびにコメントの言い回しを変えるため、本文をハッシュに含めると毎サイクル更新されてしまう。
- `fetch_summary` は日付をパスセグメント (`/wsp/advice/{date}`) で渡す。クエリパラメータ `diary_date` は無視され、常に当日のデータが返る。
- Discord への編集は `attachments` と `embeds` を常に明示して送る。省略すると古い添付や embed が残る。
- Discord への投稿・編集が失敗しても、その項目だけをスキップしてサイクルを続ける。1サイクルの失敗で常駐プロセスを落とさない (`LoginError` もログに残して次サイクルへ進む)。
- `.env`、`state/*.json`、`data/` はコミットしない。認証情報や Webhook URL を新たにログへ出さない。ただし Discord 投稿・編集失敗時の `logger.exception` (`src/main.py`) は例外文面に Webhook URL (トークンを含む) が混ざりうる。未対応のフォローアップとして扱う。
- コンテナは非 root (uid 1000) で動作する。書き込み先 (`/state`, `/data`) の権限を変える場合は `README.md` の説明も更新する。
