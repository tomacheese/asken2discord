# asken2discord

あすけんの食事記録を30分おきに取得し、Discord に Webhook 経由で投稿/編集する常駐アプリケーションです。ログインからデータ取得までを `requests` のみで行い、ブラウザ自動化は使いません。  
食事はプレーンテキスト + 画像添付、1日の栄養サマリーは embed で投稿します。

## 仕組み

- コンテナ内の `main.py` 自身が無限ループでスケジューリングを行います (Docker の cron/timer 機能やホスト側の定期実行は使いません)。
- 既定では **30分おき** (`INTERVAL_SECONDS=1800`) に、直近2日分 (`ASKEN_TRACK_DAYS=2`: 昨日・今日) × 朝食/昼食/夕食/間食 を確認します。
- 何らかの記録がある食事のみをメッセージ化します (未記入の食事は投稿しません)。
- 前回投稿時の内容ハッシュ (メニュー・写真) と比較し、変化がなければ何もしません。変化があれば:
  - まだ投稿していない (date, meal) → 新規投稿
  - 投稿済み → 同じメッセージを編集 (`message_id` は `state/state.json` に永続化)
  - 編集しようとしたメッセージが削除されていた場合は、新規投稿に切り替えます。
- 各日の栄養サマリー (コメントと栄養素ごとの摂取量) は、食事メッセージとは別の embed メッセージとして投稿します。その日の入力済みメニューのハッシュが変わったときだけ投稿・編集します (あすけんはコメントの言い回しを取得のたびに変えるため、コメント本文はハッシュに含めません)。サマリーのメッセージ ID は `state.json` の `advice` キーに保存されます。
- 取得した食事記録と日次サマリーは、Discord への投稿内容に変化がない日も含めて毎サイクル `DATA_DIR/<日付>/record.json` に保存します ([保存されるデータ](#保存されるデータ-data_dir) を参照)。
- `SIGTERM` / `SIGINT` を受け取ると、実行中のサイクルを終えてから終了します。

## メッセージの内容

### 食事メッセージ

`content` (プレーンテキスト) + 画像添付ファイルのみ。例:

```text
07/19 朝食
- 牛丼(並盛) x1 687kcal
- お造り4点盛り合わせ x1 113kcal
- エッグタルト x1 130kcal

計930kcal
```

- 1行目: `MM/DD 食事区分`
- メニュー一覧: `- メニュー名 xN kcal`。写真のみで記録されている場合は `(写真のみ記録)`
- メニューがある場合のみ `計{合計kcal}kcal` を追加
- 写真は画像ファイルとしてそのままメッセージに添付

### 日次サマリー (embed)

- タイトル: `MM/DD サマリー`
- 説明: あすけんのアドバイスコメント本文
- フィールド: 栄養素ごとに `摂取量(目安 範囲)` を表示し、名前の横に過不足の絵文字 (🔺 超過 / ✅ 適正 / 🔻 不足) を付けます。

## 保存されるデータ (`DATA_DIR`)

`DATA_DIR` (既定は `/data`) には、日付ごとのディレクトリが作られます。

```text
<DATA_DIR>/
└── 2026-11-15/
    ├── record.json   # 食事記録と日次サマリー (毎サイクル上書き)
    └── photos/       # 食事の写真 (<食事区分>_<連番>.jpg)
```

`record.json` は摂取カロリーや栄養素を後から集計・分析するための構造化データです。Discord 投稿の有無や、投稿内容の変化とは無関係に、追跡対象の日 (`ASKEN_TRACK_DAYS`) について毎サイクル書き込みます。書き込みは一時ファイル経由の原子的な置き換えです。

```json
{
  "date": "2026-11-15",
  "updated_at": "2026-11-16T09:30:00+09:00",
  "total_energy_kcal": 930,
  "meals": {
    "breakfast": {
      "label": "朝食",
      "items": [
        {
          "item_hash": "...",
          "menu_id": "...",
          "menu_name": "牛丼(並盛)",
          "quantity": "1",
          "energy_kcal": "687"
        }
      ],
      "total_energy_kcal": 687,
      "photos": ["breakfast_1.jpg"]
    },
    "lunch": {"label": "昼食", "items": [], "total_energy_kcal": 0, "photos": []},
    "dinner": {"label": "夕食", "items": [], "total_energy_kcal": 0, "photos": []},
    "sweets": {"label": "間食", "items": [], "total_energy_kcal": 0, "photos": []}
  },
  "summary": {
    "comment": "...",
    "nutrition": [
      {"name": "たんぱく質", "status": "fit", "value": "60g", "target_range": "50〜70g"}
    ]
  }
}
```

- `meals` には `breakfast` / `lunch` / `dinner` / `sweets` の 4 区分が必ず入ります。記録のない区分は `items` と `photos` が空、`total_energy_kcal` が `0` です。
- `items` はあすけんから取得した値を加工せず保存します。`quantity` と `energy_kcal` は取得時の型 (文字列・数値・`null`) のままです。
- `total_energy_kcal` (1日の合計) は、4 区分の `total_energy_kcal` の和です。
- `summary` は、その日のサマリーがなければ `null` です。`status` は `over` / `fit` / `short` のいずれかです。
- `updated_at` は JST の取得時刻で、毎サイクル更新されます。
- `photos` は写真の保存先ファイル名の命名規則から作った一覧で、ファイルの実在は確認していません。写真は Discord への投稿内容が変わったときにだけ保存するため、デプロイ前に投稿済みの日や、過去日を遡って取得した日には、ファイルが存在しないことがあります。

### 過去分の生成

`ASKEN_TRACK_DAYS` を大きくして `RUN_ONCE=1` で実行すると、過去日の `record.json` も生成できます。

```bash
docker run --rm \
  --env-file .env \
  -e RUN_ONCE=1 \
  -e ASKEN_TRACK_DAYS=31 \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/state:/state" \
  asken2discord
```

`state.json` に投稿履歴のない日は、既存の挙動どおり Discord にも新規投稿されます。投稿を避けたい場合は、`DISCORD_WEBHOOK` に投稿先として使わないチャンネルの Webhook を指定してください。

## 実行方法

`.env` をこのディレクトリ直下 (`.env`) に配置し、`ASKEN_USERNAME` / `ASKEN_PASSWORD` / `DISCORD_WEBHOOK` を設定します (`.env.example` を参照)。この `.env` は Compose の `env_file` として読み込まれます (コンテナへファイルとしてマウントはしません)。

```bash
docker compose up --build -d
docker compose logs -f
```

### 動作確認 (1サイクルだけ実行して終了)

`RUN_ONCE=1` を指定すると、1サイクルのみ実行して終了します。

```bash
docker build -t asken2discord .
docker run --rm \
  --env-file .env \
  -e RUN_ONCE=1 \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/state:/state" \
  asken2discord
```

`STATE_FILE` / `DATA_DIR` / `ASKEN_TRACK_DAYS` / `INTERVAL_SECONDS` は Dockerfile の `ENV` で既定値が設定済みのため、通常は指定不要です。上書きしたい場合のみ `-e VAR=value` を渡してください。

コンテナは非 root ユーザー (uid 1000) で動作します。ホスト側ユーザーの uid が 1000 でない場合、バインドマウントした `state` / `data` への書き込みが権限エラーになることがあります。その場合は `--user "$(id -u):$(id -g)"` を付けて実行してください。

## 設定 (環境変数)

| 変数 | 既定値 | 説明 |
|---|---|---|
| `ASKEN_USERNAME` | (必須) | あすけんのログインメールアドレス |
| `ASKEN_PASSWORD` | (必須) | あすけんのログインパスワード |
| `DISCORD_WEBHOOK` | (必須) | 投稿先の Discord Webhook URL |
| `ASKEN_TRACK_DAYS` | `2` | 追跡する日数 (今日を含めて遡る日数) |
| `INTERVAL_SECONDS` | `1800` | 取得サイクルの間隔 (秒) |
| `STATE_FILE` | `/state/state.json` | 投稿済みメッセージの状態を保存するファイル |
| `DATA_DIR` | `/data` | 取得したデータ (`record.json`)・写真のローカル保存先 |
| `RUN_ONCE` | (未設定) | `1`/`true` を指定すると1サイクルのみ実行して終了 (動作確認用) |
| `SENTRY_DSN` | (未設定) | Sentry/GlitchTip の DSN。設定すると `logger.exception()` で捕捉した未処理エラーが送信される (未設定時は送信自体が無効化される) |

必須の環境変数 (`ASKEN_USERNAME` / `ASKEN_PASSWORD` / `DISCORD_WEBHOOK`) が未設定の場合、アプリは起動時にエラー終了します。

## ライセンス

[MIT License](./LICENSE)
