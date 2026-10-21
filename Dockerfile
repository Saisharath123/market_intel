FROM public.ecr.aws/amazonlinux/amazonlinux:2023
RUN dnf upgrade --releasever=latest -y && dnf install -y python3.11 python3.11-pip shadow-utils \
    && dnf clean all && ln -s /usr/bin/python3.11 /usr/local/bin/python
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY market market
COPY app.py .
RUN useradd --create-home appuser
USER appuser
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)"
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.gatherUsageStats=false"]
