"""Fixed unauthenticated GET operations only. No SDK, env, proxies or redirects."""
import http.client
import json
import ssl
from urllib.parse import urlencode

from backtest.data import Candle, SYMBOLS, STEP_MS, _timestamp

HOST = 'fapi.binance.com'
MAX_BYTES = 2_000_000


class TemporaryPublicError(ValueError):
    def __init__(self, *, retry_after_s=0):
        self.retry_after_s=retry_after_s
        super().__init__('Temporary public market-data failure')


def _read(path):
    # Only the two typed operations below call this transport. No arbitrary host,
    # authentication/header arguments, URL input or redirect following exists.
    if path != '/fapi/v1/time' and not path.startswith('/fapi/v1/klines?'):
        raise ValueError('Public route denied')
    connection = http.client.HTTPSConnection(HOST, timeout=10)
    try:
        connection.request('GET', path, headers={'Accept': 'application/json'})
        response = connection.getresponse()
        if response.status in (408,429,500,502,503,504):
            header=response.getheader('Retry-After')
            retry=900 if response.status==429 else 0
            if header is not None:
                if not isinstance(header,str) or not header.isascii() or not header.isdigit():
                    raise ValueError('Unrecognized Retry-After; fail closed')
                retry=max(retry,int(header))
            raise TemporaryPublicError(retry_after_s=retry)
        if response.status != 200:
            raise ValueError('Public endpoint unavailable; no fallback or redirect')
        raw = response.read(MAX_BYTES+1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Public response exceeds bound')
        return json.loads(raw, parse_float=lambda _: (_ for _ in ()).throw(ValueError('Unexpected float')))
    except TemporaryPublicError:
        raise
    except ssl.SSLError:
        raise ValueError('Public market-data request failed') from None
    except (OSError, http.client.RemoteDisconnected, http.client.IncompleteRead):
        raise TemporaryPublicError() from None
    except Exception:
        # Do not interpolate remote payloads, URLs, exception text or headers.
        raise ValueError('Public market-data request failed') from None
    finally:
        connection.close()


def server_time():
    body = _read('/fapi/v1/time')
    if type(body) is not dict or set(body) != {'serverTime'} or type(body['serverTime']) is not int:
        raise ValueError('Malformed public time')
    return _timestamp(body['serverTime'])


def closed_klines(symbol, start, end, *, now):
    """One bounded page from [start,end); caller advances only durable cursors."""
    if (symbol not in SYMBOLS or any(type(v) is not int for v in (start, end, now))
            or start % STEP_MS or end % STEP_MS or not start < end <= now//STEP_MS*STEP_MS):
        raise ValueError('Only closed aligned public 15m requests allowed')
    _timestamp(start); _timestamp(end); _timestamp(now)
    end = min(end, start+1000*STEP_MS)
    body = _read('/fapi/v1/klines?'+urlencode(dict(symbol=symbol, interval='15m',
        startTime=start, endTime=end-1, limit=1000)))
    if type(body) is not list or len(body) != (end-start)//STEP_MS:
        raise ValueError('Incomplete public candle page')
    candles = []
    for i, row in enumerate(body):
        if (type(row) is not list or len(row) != 12 or type(row[0]) is not int
                or type(row[6]) is not int or row[0] != start+i*STEP_MS
                or row[6] >= now or any(type(row[j]) is not str for j in (1,2,3,4,5))):
            raise ValueError('Malformed, future or misordered public candle')
        candles.append(Candle(symbol, '15m', row[0], row[6], *row[1:6]))
    return tuple(candles)
