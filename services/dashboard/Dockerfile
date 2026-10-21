FROM public.ecr.aws/amazonlinux/amazonlinux:2023
RUN dnf upgrade --releasever=latest -y && dnf install -y python3.11 python3.11-pip shadow-utils \
    && dnf clean all && ln -s /usr/bin/python3.11 /usr/local/bin/python
WORKDIR /app
COPY services/dashboard/requirements.txt requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 appuser
ENV PATH="/usr/local/bin:/usr/bin:/bin"
COPY market market
COPY app.py .
USER 10001
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.gatherUsageStats=false"]
