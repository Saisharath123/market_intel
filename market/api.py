from fastapi import FastAPI
from market.service import insights, news, quotes

app = FastAPI(title='India Market Pulse', version='0.1.0',
              description='Read-only MVP. Demo values are synthetic. Inspect mode, status and timestamps.')


@app.get('/health')
def health():
    return {'status': 'ok', 'meaning': 'Process alive; does not verify upstream providers'}


@app.get('/api/v1/quotes')
def get_quotes():
    return quotes()


@app.get('/api/v1/news')
def get_news():
    return news()


@app.get('/api/v1/insights')
def get_insights():
    snapshot = quotes()
    return {k: snapshot[k] for k in ('mode', 'status', 'source', 'fetched_at', 'error')} | insights(snapshot)
