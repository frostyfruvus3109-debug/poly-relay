import os
import json
from flask import Flask, request, Response
import requests

app = Flask(__name__)
TARGET = "https://clob.polymarket.com"

# Canonical POLY_* header names Polymarket expects
POLY_FIELDS = [
    'address', 'signature', 'timestamp', 'nonce',
    'api-key', 'api_key', 'passphrase',
]

def normalize_poly_header(key: str):
    """
    Accept POLY_ADDRESS, poly_address, Poly_Address, poly-address, POLY-ADDRESS etc.
    Returns the canonical 'POLY_ADDRESS' form, or None if not a POLY header.
    """
    k = key.strip().lower()
    # strip leading 'poly' prefix with any separator
    for prefix in ('poly_', 'poly-', 'poly'):
        if k.startswith(prefix):
            suffix = k[len(prefix):]
            # Normalize the suffix: replace hyphens with underscores
            suffix = suffix.replace('-', '_')
            return f'POLY_{suffix.upper()}'
    return None

SKIP = {'host', 'content-length', 'transfer-encoding'}


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])
def relay(path):
    headers = {}
    for k, v in request.headers:
        kl = k.lower()
        if kl in SKIP:
            continue
        canonical = normalize_poly_header(k)
        if canonical:
            headers[canonical] = v
        else:
            headers[k] = v

    headers['User-Agent'] = (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    )

    url = f"{TARGET}/{path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"

    try:
        resp = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=request.get_data(),
            timeout=20,
        )
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get('Content-Type', 'application/json'),
        )
    except Exception as e:
        return Response(str(e), status=500)


@app.route('/ok')
@app.route('/health')
def health():
    return 'OK'


@app.route('/time')
def server_time():
    import time
    return str(int(time.time()))


@app.route('/debug', methods=['GET', 'POST'])
def debug():
    """Echo back all received headers (before remap) for debugging."""
    return Response(
        json.dumps(dict(request.headers), indent=2),
        content_type='application/json',
    )


@app.route('/debug-forward', methods=['GET', 'POST'])
def debug_forward():
    """Show what headers would be sent to Polymarket after remap."""
    out = {}
    for k, v in request.headers:
        kl = k.lower()
        if kl in SKIP:
            continue
        canonical = normalize_poly_header(k)
        out[canonical if canonical else k] = v
    return Response(json.dumps(out, indent=2), content_type='application/json')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
