# Образ для Render: бот, сбор и разбор в одном процессе.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Chromium нужен только для Facebook. С датацентрового адреса FB почти наверняка
# встретит стеной логина (он уже закрыт даже для домашнего IP), поэтому по умолчанию
# браузер не ставим — это минус ~500 МБ образа и минуты сборки.
# Нужен Facebook — собирай с --build-arg INSTALL_CHROMIUM=true.
ARG INSTALL_CHROMIUM=false
RUN if [ "$INSTALL_CHROMIUM" = "true" ]; then python -m playwright install --with-deps chromium; fi

COPY zlinbot ./zlinbot
COPY scripts ./scripts

# Данные лежат на подключённом диске: без него SQLite и кэш медиа пропадут при первом же деплое.
ENV DB_PATH=/data/zlinbot.db \
    MEDIA_DIR=/data/media \
    DEBUG_DIR=/data/debug

CMD ["python", "scripts/bot.py"]
