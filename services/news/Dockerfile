FROM public.ecr.aws/amazonlinux/amazonlinux:2023
RUN dnf upgrade --releasever=latest -y && dnf install -y python3.11 python3.11-pip shadow-utils \
    && dnf clean all && ln -s /usr/bin/python3.11 /usr/local/bin/python
WORKDIR /app
COPY services/news/requirements.txt requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 appuser
COPY market market
USER 10001
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "market.news_api:app", "--host", "0.0.0.0", "--port", "8000"]
