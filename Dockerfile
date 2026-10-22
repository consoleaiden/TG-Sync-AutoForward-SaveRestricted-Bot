FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# config.yaml / data / sessions 不进镜像（见 .dockerignore），运行时由宿主机挂载
COPY . .

RUN mkdir -p /app/data /app/sessions

# OCI 标准标签：GHCR 会自动关联源码仓库，也便于 Watchtower 等工具识别
LABEL org.opencontainers.image.title="TG-Sync AutoForward SaveRestricted Bot" \
      org.opencontainers.image.description="Telethon-based Telegram forwarding bot with Bot+UserBot dual-client architecture" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/consoleaiden/TG-Sync-AutoForward-SaveRestricted-Bot"

CMD ["python", "main.py"]
