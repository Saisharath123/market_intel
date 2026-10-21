"""Keyless Yahoo adapter. Upstream access is unofficial and may be delayed."""
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import yfinance as yf

SYMBOLS = {'NSE:NIFTY 50': '^NSEI', 'NSE:NIFTY BANK': '^NSEBANK',
           'NSE:RELIANCE': 'RELIANCE.NS', 'NSE:TCS': 'TCS.NS',
           'NSE:HDFCBANK': 'HDFCBANK.NS', 'NSE:INFY': 'INFY.NS'}
IST = ZoneInfo('Asia/Kolkata')


def normalize(symbol, history, metadata):
    if history.empty or metadata.get('currency') != 'INR':
        raise ValueError('Missing INR market data')
    history = history.dropna(subset=['Close']).sort_index()
    if history.empty:
        raise ValueError('No closing prices')
    raw_time = metadata.get('regularMarketTime')
    if isinstance(raw_time, (int, float)):
        quote_time = datetime.fromtimestamp(raw_time, timezone.utc)
    elif isinstance(raw_time, datetime) and raw_time.tzinfo is not None:
        quote_time = raw_time
    else:
        raise ValueError('No reliable quote timestamp')
    price = float(metadata['regularMarketPrice'])
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Invalid price')
    if quote_time > datetime.now(timezone.utc):
        raise ValueError('Future quote timestamp')
    session = quote_time.astimezone(IST).date()
    # Compare the quote with a prior trading session, never the first close
    # of a multi-day chart (chartPreviousClose is the range baseline).
    prior = history[[date.date() < session for date in history.index]]
    close = float(prior.iloc[-1]['Close']) if not prior.empty else None
    if close is not None and (not math.isfinite(close) or close <= 0):
        close = None
    return dict(symbol=symbol, provider_symbol=SYMBOLS[symbol], price=price,
                previous_close=close, change_pct=round((price / close - 1) * 100, 2) if close else None,
                quote_time=quote_time.astimezone(IST).isoformat(), session_date=session.isoformat(),
                volume=metadata.get('regularMarketVolume'), currency='INR',
                delayed=True, age_hours=round((datetime.now(timezone.utc) - quote_time).total_seconds() / 3600, 1))


def fetch_one(pair):
    symbol, provider_symbol = pair
    try:
        ticker = yf.Ticker(provider_symbol)
        history = ticker.history(period='5d', interval='1d', auto_adjust=False, timeout=8)
        return normalize(symbol, history, ticker.get_history_metadata())
    except Exception:
        # yfinance exposes provider/network/format exceptions from several
        # libraries; a single failed symbol must not take down the watchlist.
        return None


def fetch_quotes():
    with ThreadPoolExecutor(max_workers=3) as pool:
        return [row for row in pool.map(fetch_one, SYMBOLS.items()) if row is not None]
