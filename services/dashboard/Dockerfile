# syntax=docker/dockerfile:1.7
ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023
FROM ${AL2023_IMAGE} AS runtime-base
RUN dnf upgrade --releasever=latest -y \
    && dnf install -y python3.11 ca-certificates libstdc++ shadow-utils \
    && dnf clean all \
    && ln -s /usr/bin/python3.11 /usr/local/bin/python \
    && useradd --create-home --uid 10001 appuser
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"
WORKDIR /app

FROM runtime-base AS builder
# pip and wheel caches exist only in this discarded build stage.
RUN dnf install -y python3.11-pip python3.11-devel gcc gcc-c++ sqlite-devel \
    && python -m venv /opt/venv
COPY services/dashboard/requirements.txt /build/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv/bin/python -m pip install --upgrade pip \
    && /opt/venv/bin/python -m pip wheel --wheel-dir=/wheels -r /build/requirements.txt \
    && /opt/venv/bin/python -m pip install --no-index --find-links=/wheels -r /build/requirements.txt \
    && /opt/venv/bin/python -m pip uninstall --yes pip setuptools wheel

FROM runtime-base AS runtime
COPY --from=builder /opt/venv /opt/venv
COPY market market
COPY app.py .
USER 10001:10001
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)"
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.gatherUsageStats=false"]
