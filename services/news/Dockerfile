FROM python:3.11-slim
WORKDIR /app
COPY services/news/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home --uid 10001 appuser
COPY market market
USER 10001
EXPOSE 8000
CMD ["uvicorn", "market.news_api:app", "--host", "0.0.0.0", "--port", "8000"]
