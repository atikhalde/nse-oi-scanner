"""M15: opening-range failed-break reclaim. Pure, causal 5-minute bar detector.

A fresh price-action hypothesis, NOT a fitted/high-accuracy claim. The first six
bars define a range; after a sweep outside it, require a close back inside and
positive (negative) candle for BUY (SELL). Never inspect future bars.
"""

def candidate(bars, j):
    if j < 7:
        return None
    t = str(bars['t'].iloc[j])
    if not '09:50' <= t <= '14:30':
        return None
    # Do not treat a truncated or late feed as a valid opening range.
    if list(bars['t'].iloc[:6]) != ['09:15', '09:20', '09:25', '09:30', '09:35', '09:40']:
        return None
    base = bars.iloc[:6]
    top, bottom = float(base.high.max()), float(base.low.min())
    if top <= bottom:
        return None
    cur = bars.iloc[j]
    close, op, high, low = (float(cur[x]) for x in ('close', 'open', 'high', 'low'))
    width = top - bottom
    # Wick sweep + reclaim on the same completed candle; direction confirms.
    if low < bottom and bottom < close < top and close > op:
        strength = (close - bottom) / width
        side = 'BUY'
    elif high > top and bottom < close < top and close < op:
        strength = (top - close) / width
        side = 'SELL'
    else:
        return None
    if strength < 0.35:
        return None
    return {'side': side, 'score': strength, 'signal': 'M15 RANGE RECLAIM'}
