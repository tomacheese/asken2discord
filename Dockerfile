FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATE_FILE=/state/state.json \
    DATA_DIR=/data \
    ASKEN_TRACK_DAYS=2 \
    INTERVAL_SECONDS=1800

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# uid 1000 は一般的なシングルユーザーのホストの既定値に合わせている。これにより
# --user を指定しなくても、バインドマウントした state / data ディレクトリに書き込める。
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /state /data \
    && chown -R app:app /state /data
USER app

ENTRYPOINT ["./entrypoint.sh"]
