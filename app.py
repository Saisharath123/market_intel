import pandas as pd
import streamlit as st
from market.service import insights, news, quotes

st.set_page_config(page_title='India Market Pulse', page_icon='📈', layout='wide')
st.title('India Market Pulse')
st.caption('Indian indices, a small stock watchlist, and market headlines')
st.sidebar.header('MVP controls')
st.sidebar.caption('Quotes and news: 5-minute cache')
st.sidebar.caption('Source modes are configured on the server in .env. Restart after changing them.')
refresh = st.sidebar.toggle('Refresh while this tab is open', value=True)
st.sidebar.button('Refresh now')
st.sidebar.caption('Read-only dashboard. No trading or account access.')


@st.fragment(run_every=30 if refresh else None)
def dashboard():
    snapshot = quotes()
    if snapshot['mode'] == 'demo':
        st.warning('DEMO QUOTES — fixed synthetic values, not current market prices.')
    elif snapshot['mode'] == 'yahoo':
        st.info('Actual market prices from Yahoo Finance · may be delayed · refreshes every five minutes · no API key required.')
    if snapshot['error']:
        st.error(snapshot['error'])
    st.caption(f"Quote source: {snapshot['source']} · Status: {snapshot['status']} · Fetched (UTC): {snapshot['fetched_at']}")
    st.caption('Fetched time is not exchange quote time. Prices may be delayed or from the previous session; see quote_time below.')
    items = snapshot['items']
    if items:
        cols = st.columns(3)
        for i, item in enumerate(items):
            delta = item['change_pct']
            cols[i % 3].metric(item['symbol'].split(':')[1], f"₹{item['price']:,.2f}",
                               f'{delta:+.2f}%' if delta is not None else None,
                               help=f"Quote time: {item.get('quote_time') or 'unavailable'} · Change vs previous trading-session close")
        if snapshot['missing_symbols']:
            st.warning('Missing quotes: ' + ', '.join(snapshot['missing_symbols']))
        st.subheader('Watchlist insights')
        summary = insights(snapshot)
        st.write(f"{summary['advances']} advancing · {summary['declines']} declining · {summary['unchanged']} unchanged")
        st.caption(summary['scope'])
        frame = pd.DataFrame(summary['leaders'])
        if not frame.empty:
            st.bar_chart(frame.set_index('symbol')['change_pct'], horizontal=True)
        with st.expander('Quote details and exchange timestamps'):
            st.dataframe(pd.DataFrame(items), width='stretch', hide_index=True)
    else:
        st.info('No quotes available. Headlines can still load independently.')
    st.subheader('Market headlines')
    headlines = news()
    st.caption(f"Source: {headlines['source']} · Status: {headlines['status']} · Fetched (UTC): {headlines['fetched_at']}")
    if headlines['mode'] == 'demo':
        st.info('DEMO NEWS — synthetic headline for UI preview.')
    if headlines['error']:
        st.error(headlines['error'])
    query = st.text_input('Filter headlines', placeholder='Company or keyword')
    for item in headlines['items']:
        if query.casefold() not in item['title'].casefold():
            continue
        st.text(item['title'])
        st.caption(f"{item['source']} · {item['published_at'] or 'No publication timestamp'}")
        if item['url']:
            st.link_button('Read at source', item['url'])


dashboard()
