# TradingAgents-CN 单镜像 Dockerfile
# ------------------------------------------------------------------------------
# 同一镜像内运行：nginx (端口 80, 负责静态 + /api 反代) + FastAPI (uvicorn 8000)
# 进程管理：supervisord
# 多架构：amd64 / arm64（pandoc 与 wkhtmltopdf 依据 TARGETARCH 选包）
# 用途：阿里云 ACR 一条构建规则即可推一个镜像，K8s/ACI/SAE/ECS 直接拉取
# ------------------------------------------------------------------------------

# ============================================================================
# Stage 1: 构建前端静态产物
# ============================================================================
FROM node:22-alpine AS frontend-build

ENV NODE_ENV=production
WORKDIR /app/frontend

RUN corepack enable && corepack prepare yarn@1.22.22 --activate

# 先装依赖（利用 layer cache）
COPY frontend/package.json frontend/yarn.lock frontend/.yarnrc ./
RUN yarn install --frozen-lockfile --production=false --network-timeout 300000

# 源码 + 静态资源
COPY frontend/. ./
COPY assets /app/frontend/public/assets
COPY docs /app/docs

RUN yarn vite build


# ============================================================================
# Stage 2: 后端运行时 + nginx + supervisord
# ============================================================================
FROM python:3.10-slim-bookworm AS runtime

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    TZ=Asia/Shanghai \
    DOCKER_CONTAINER=true

WORKDIR /app

# ---- 系统依赖：nginx / supervisord / pandoc / wkhtmltopdf / 中文字体 ----
RUN mkdir -p /app/logs /app/data /app/config /var/log/supervisor && \
    echo 'Acquire::Retries "3";' > /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::http::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \
    echo 'Acquire::https::Timeout "30";' >> /etc/apt/apt.conf.d/80-retries && \
    (apt-get update || apt-get update || apt-get update) && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        xvfb \
        nginx \
        supervisor \
        fontconfig \
        fonts-noto-cjk && \
    if [ "$TARGETARCH" = "arm64" ]; then \
        ARCH="arm64"; \
    else \
        ARCH="amd64"; \
    fi && \
    wget -q https://github.com/jgm/pandoc/releases/download/3.8.2.1/pandoc-3.8.2.1-1-${ARCH}.deb && \
    dpkg -i pandoc-3.8.2.1-1-${ARCH}.deb && \
    rm pandoc-3.8.2.1-1-${ARCH}.deb && \
    wget -q https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.bookworm_${ARCH}.deb && \
    apt-get install -y --no-install-recommends \
        ./wkhtmltox_0.12.6.1-3.bookworm_${ARCH}.deb && \
    rm wkhtmltox_0.12.6.1-3.bookworm_${ARCH}.deb && \
    fc-cache -fv && \
    rm -rf /var/lib/apt/lists/* && \
    rm -f /etc/nginx/sites-enabled/default

# ---- Python 依赖 ----
# pyproject.toml 里 setuptools.packages.find = ["tradingagents*"]，
# 所以 pip install . 需要先看到 tradingagents/ 目录。
# 把包源码与 pyproject.toml 一起作为依赖层 — 这两块变才会重装。
COPY pyproject.toml README.md ./
COPY tradingagents ./tradingagents
RUN pip install --upgrade pip && \
    pip install --prefer-binary . && \
    pip install --prefer-binary pdfkit

# ---- 其余后端代码（频繁变动，放在依赖层之后） ----
COPY app ./app
COPY config ./config
COPY scripts ./scripts
COPY docs ./docs
COPY install ./install
COPY .env.docker ./.env

# ---- 前端构建产物 ----
COPY --from=frontend-build /app/frontend/dist /usr/share/nginx/html

# ---- nginx + supervisord 配置 ----
COPY docker/nginx.conf       /etc/nginx/conf.d/default.conf
COPY docker/supervisord.conf /etc/supervisor/conf.d/tradingagents.conf

# 对外只暴露 80（nginx），内部 8000 不外露，由 nginx 反代
EXPOSE 80

# 健康检查走 nginx 提供的 /health
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost/health || exit 1

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
