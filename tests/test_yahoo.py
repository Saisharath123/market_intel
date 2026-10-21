from datetime import datetime, timezone

import pandas as pd
import pytest

from market import service, yahoo


def fixture_data():
    history = pd.DataFrame({'Close': [80.0, 100.0, 110.0]},
                           index=pd.to_datetime(['2024-04-19', '2024-04-22', '2024-04-23']).tz_localize('Asia/Kolkata'))
    metadata = {'currency': 'INR', 'regularMarketPrice': 112,
                'regularMarketTime': datetime(2024, 4, 23, 9, 0, tzinfo=timezone.utc),
                'chartPreviousClose': 75, 'regularMarketVolume': 1234}
    return history, metadata


def test_change_uses_prior_session_not_chart_baseline_or_same_day():
    history, metadata = fixture_data()
    row = yahoo.normalize('NSE:RELIANCE', history, metadata)
    assert row['price'] == 112
    assert row['previous_close'] == 100
    assert row['change_pct'] == 12
    assert row['quote_time'] == '2024-04-23T14:30:00+05:30'
    assert row['session_date'] == '2024-04-23'
    assert row['delayed'] is True


@pytest.mark.parametrize('field,value', [('currency', 'USD'), ('regularMarketPrice', float('nan')),
                                         ('regularMarketTime', None)])
def test_invalid_quotes_are_rejected(field, value):
    history, metadata = fixture_data()
    metadata[field] = value
    with pytest.raises(ValueError):
        yahoo.normalize('NSE:RELIANCE', history, metadata)


def test_partial_and_total_outage_never_return_demo(monkeypatch):
    monkeypatch.setenv('MARKET_MODE', 'yahoo')
    history, metadata = fixture_data()
    row = yahoo.normalize('NSE:RELIANCE', history, metadata)
    monkeypatch.setattr(yahoo, 'fetch_quotes', lambda: [row])
    result = service.quotes.__wrapped__()
    assert result['status'] == 'partial'
    assert len(result['missing_symbols']) == 5
    assert result['items'][0]['price'] == 112
    monkeypatch.setattr(yahoo, 'fetch_quotes', lambda: [])
    result = service.quotes.__wrapped__()
    assert result['mode'] == 'yahoo'
    assert result['status'] == 'unavailable'
    assert result['items'] == []
