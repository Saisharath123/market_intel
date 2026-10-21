from fastapi import FastAPI
from market.service import insights, quotes
from market.metrics import instrument

app = FastAPI(title='Market Intel Market API', version='0.2.0')
instrument(app, 'market')


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'market'}


@app.get('/api/v1/quotes')
def get_quotes():
    return quotes()


@app.get('/api/v1/insights')
def get_insights():
    snapshot = quotes()
    return {k: snapshot[k] for k in ('mode', 'status', 'source', 'fetched_at', 'error')} | insights(snapshot)
