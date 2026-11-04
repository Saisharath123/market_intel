from fastapi import FastAPI
from market.service import news
from market.metrics import instrument
from market.telemetry import configure_telemetry

app = FastAPI(title='Market Intel News API', version='0.2.0')
instrument(app, 'news')
configure_telemetry('market-intel-news', app)


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'news'}


@app.get('/api/v1/news')
def get_news():
    return news()
