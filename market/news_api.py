from fastapi import FastAPI
from market.service import news

app = FastAPI(title='Market Intel News API', version='0.2.0')


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'news'}


@app.get('/api/v1/news')
def get_news():
    return news()
