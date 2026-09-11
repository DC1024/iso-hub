# ---- ISO Hub 镜像 ----
# 对上游 Sowevo/iso_download(纯CLI) 的 Web 化封装
# 目录结构:
#   /app/iso_download   上游脚本(只读,不可变)
#   /app/web            Flask API + 前端 + 选择性下载 runner
#   /data               [VOLUME] 发行版清单 distributions.json + 下载的 ISO
#
# ⚠️ 安全提示:
#   1. 本镜像默认以 root 运行。新版 compose 中主容器不再挂载宿主机 /var/run/docker.sock,
#      而是设 DOCKER_HOST=tcp://socket-proxy:2375 经 socket-proxy(tecnativa/docker-socket-proxy)
#      按白名单访问 /containers/* 端点, 权限已收窄。仍请仅在可信内网使用。
#   2. 生产部署务必设置 ISO_HUB_TOKEN / ISO_HUB_REQUIRE_LOGIN=1, 并修改默认共享/种子凭据。
#   3. 旧版部署(DOCKER_HOST 未设置)会回退到 Unix socket /var/run/docker.sock, 保持向后兼容。
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

# 安装 gpg/gnupg: 用于发行版 checksum 文件的 GPG 签名验证(P2/P3 功能依赖)
# 国内构建可传 --build-arg APT_MIRROR=mirrors.aliyun.com 使用镜像加速
ARG APT_MIRROR=archive.ubuntu.com
RUN sed -i "s@//.*archive.ubuntu.com@//${APT_MIRROR}@; s@//security.ubuntu.com@//${APT_MIRROR}@g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends gnupg gpgv \
    && rm -rf /var/lib/apt/lists/*

# 上游 CLI 依赖
COPY iso_download/requirements.txt /app/iso_download/requirements.txt
RUN pip install --no-cache-dir -r /app/iso_download/requirements.txt

# 上游脚本本体
COPY iso_download/ /app/iso_download/

# Web 层
COPY web/ /app/web/
RUN pip install --no-cache-dir -r /app/web/requirements.txt

# 运行期瘦身: 卸载构建期才用得到的 pip(依赖已在上面装好, 运行时不再需要)。
# 目的:
#   ① 减小镜像体积(去 pip + ensurepip + 缓存)
#   ② 消除 python-pip 的一批 CVE —— 2025-8869 / 2026-13346 / 2026-6357 /
#      2026-3219 / 2026-8643 / 2026-1703。这些全是"用 pip 安装不可信来源的包"
#      这一场景才可能触发, 而 iso-hub **运行期从不执行 pip install**
#      (依赖在构建阶段固定安装, CMD 只跑 python app.py)。Debian 也对这批全部
#      标注 <no-dsa>(次要问题, 不单独发补丁), 因此移除 pip 是根除而非掩盖。
# 保留 setuptools: 少数已装包在 import 时仍可能引用 pkg_resources。
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip \
              /usr/local/lib/python3.12/site-packages/pip \
              /usr/local/lib/python3.12/site-packages/pip-* \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
              /root/.cache/pip

# 数据卷：distributions.json + 下载目录(linux/<发行版>/) 持久化
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/api/health',timeout=4)" || exit 1

WORKDIR /app/web
CMD ["python", "app.py"]
