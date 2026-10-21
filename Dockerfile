# ---- ISO Hub 镜像 ----
# 对上游 Sowevo/iso_download(纯CLI) 的 Web 化封装
# 目录结构:
#   /app/iso_download   上游脚本(只读,不可变)
#   /app/web            Flask API + 前端 + 选择性下载 runner
#   /data               [VOLUME] 发行版清单 distributions.json + 下载的 ISO
#
# ⚠️ 安全提示:
#   1. v1.5.0 起本镜像以 **非 root(uid/gid 10001)** 运行, 且 compose 里配合
#      read_only: true + tmpfs /tmp。运行期唯一可写的位置是显式挂载的卷:
#      /data、/qb-config、/webdav-config、/samba-config 与内存盘 /tmp。
#      ⇒ 从 1.4.x 升级必须先在宿主机把挂载目录交给 10001:
#           chown -R 10001:10001 ./data ./qb-config ./webdav-config ./samba-config
#      否则启动自检会立即退出并打印这条命令(不会静默半死不活)。
#   2. 主容器不挂载宿主机 /var/run/docker.sock, 而是设 DOCKER_HOST=tcp://socket-proxy:2375
#      经 socket-proxy 按静态白名单访问 /containers/* 端点。仍请仅在可信内网使用。
#   3. 生产部署务必设置 ISO_HUB_TOKEN / ISO_HUB_REQUIRE_LOGIN=1, 并修改默认共享/种子凭据。
#   4. 旧版部署(DOCKER_HOST 未设置)会回退到 Unix socket /var/run/docker.sock, 保持向后兼容。
# 基础镜像钉 digest(v1.5.3): `python:3.12-slim` 是浮动 tag, 上游每发一次补丁
# 重建出来的镜像就变一次 —— 同一个 commit 两次构建产物不同, 无法复现也无法对比 CVE。
# tag 保留 3.12.15 是为了可读(人一眼看出 Python 版本), digest 才是真正生效的那半。
# 升级: 改 tag 后执行
#   docker pull python:3.12.15-slim && docker inspect --format='{{index .RepoDigests 0}}' python:3.12.15-slim
# 把新 digest 粘回来, 再跑一遍 Container Smoke。
FROM python:3.12.15-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2

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
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends gnupg gpgv \
    && rm -rf /var/lib/apt/lists/*

# 依赖锁定: constraints.txt 把版本钉到生产实测值, 否则 `>=` 会让同一 commit 的
# 两次构建装到不同的包(详见文件头注释)。必须 COPY 在两条 pip install 之前。
COPY constraints.txt /app/constraints.txt

# 上游 CLI 依赖
COPY iso_download/requirements.txt /app/iso_download/requirements.txt
RUN pip install --no-cache-dir -c /app/constraints.txt -r /app/iso_download/requirements.txt

# 上游脚本本体
COPY iso_download/ /app/iso_download/

# Web 层
COPY web/ /app/web/
RUN pip install --no-cache-dir -c /app/constraints.txt -r /app/web/requirements.txt

# 运维脚本(备份/恢复)。
# 随镜像分发是为了让 `docker exec iso-hub python /app/scripts/backup.py` 直接可用 ——
# 宿主上不必装 Python, 也不必去猜 /data 在宿主机的真实绑定路径(极空间是
# /tmp/zfsv3/... 这种, `docker inspect` 才能查到)。
COPY scripts/ /app/scripts/

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

# ---- 非 root 运行(v1.5.0) ----
# 为什么是固定 uid/gid 10001 而不是 "user: nobody": 绑定挂载(bind mount)的属主由宿主机
# 决定, 容器内看不到 /etc/passwd 也不会改变它。固定一个数值 UID 才能让「宿主机上
# chown -R 10001:10001 ./data」与容器内进程对上号(见 README「非 root 运行与升级迁移」)。
# 家目录只用于 HOME/GNUPGHOME: 代码里每次调 gpg 都已显式把 GNUPGHOME 指到 tempfile
# 临时目录, 但 `gpg --show-keys` 这类"看起来只读"的调用也会在 homedir 里创建
# pubring.kbx —— 所以 compose 里把 /home/isohub 额外挂成了内存盘(tmpfs)。
# 镜像里先建好并 chown, 是为了 `docker run` 不带 tmpfs 时也能正常写。
RUN groupadd -g 10001 isohub \
    && useradd -u 10001 -g 10001 -d /home/isohub -m -s /usr/sbin/nologin isohub \
    && mkdir -p /data /home/isohub/.gnupg \
    && chmod 700 /home/isohub/.gnupg \
    && chown -R 10001:10001 /data /home/isohub

# HOME 必须显式设置: python:3.12-slim 里 uid 10001 没有 passwd 之外的线索,
# 而 gpg 在 GNUPGHOME 缺失时会往 $HOME/.gnupg 写 —— 根文件系统只读时那就是硬失败。
ENV HOME=/home/isohub \
    GNUPGHOME=/home/isohub/.gnupg

# 数据卷：distributions.json + 下载目录(linux/<发行版>/) 持久化
# 注意: 匿名卷会继承镜像里的 /data 属主(10001); 但 compose 用的是**绑定挂载**,
# 属主以宿主机目录为准 —— 所以升级时要 chown。
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8080/api/health',timeout=4)" || exit 1

# 降权放在最后: 上面所有 apt/pip/文件复制都需要 root。
USER 10001:10001

WORKDIR /app/web
CMD ["python", "app.py"]
