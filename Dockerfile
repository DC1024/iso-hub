# ---- ISO Hub 镜像 ----
# 对上游 Sowevo/iso_download(纯CLI) 的 Web 化封装
# 目录结构:
#   /app/iso_download   上游脚本(只读,不可变)
#   /app/web            Flask API + 前端 + 选择性下载 runner
#   /data               [VOLUME] 发行版清单 distributions.json + 下载的 ISO
FROM python:3.12-slim

# 国内构建可传 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=$PIP_INDEX_URL

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ISO_REPO_DIR=/app/iso_download \
    ISO_DATA_DIR=/data \
    ISO_HUB_PORT=8080

WORKDIR /app

# 上游 CLI 依赖
COPY iso_download/requirements.txt /app/iso_download/requirements.txt
RUN pip install --no-cache-dir -r /app/iso_download/requirements.txt

# 上游脚本本体
COPY iso_download/ /app/iso_download/

# Web 层
COPY web/ /app/web/
RUN pip install --no-cache-dir -r /app/web/requirements.txt

# 数据卷：distributions.json + 下载目录(linux/<发行版>/) 持久化
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/api/health',timeout=4)" || exit 1

WORKDIR /app/web
CMD ["python", "app.py"]
