"""HTTP checks of the isolated localhost stack. Never print credentials or tokens."""
import base64
import hashlib
import json
import secrets
import re
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlencode

config = dict(line.split('=', 1) for line in (Path(__file__).resolve().parents[1] / '.env.audit').read_text().splitlines()
              if '=' in line and not line.startswith('#'))
assert config.get('LOCAL_AUDIT', '').lower() == 'true', 'Only the isolated local audit environment is allowed.'
base = 'http://127.0.0.1:8030'
local_http = build_opener(ProxyHandler({}))


def request(path, data=None, *, headers=None, form=False):
    headers = dict(headers or {})
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        data = urlencode(data).encode() if form else json.dumps(data).encode()
    with local_http.open(Request(path if path.startswith('http') else base + path, data=data, headers=headers), timeout=30) as response:
        body = response.read()
        return response.status, response.headers, body


for page in ['home', 'insights', 'settings', 'documents']:
    status, _, body = request('http://127.0.0.1:4300/app/' + page)
    assert status == 200 and b'<app-root' in body
    print('PASS frontend route:', page)
main_module = re.search(r'src="([^"]*main[^"/]*\.js)"', body.decode()).group(1)
status, _, body = request('http://127.0.0.1:4300/' + main_module.lstrip('/'))
assert status == 200 and b'node_modules/primeng' not in body
print('PASS frontend JavaScript delivery')
verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
status, headers, body = request('/api/v1/auth/browser/authorize/', {
    'email': 'admin@harperlowe.demo', 'password': config['AUDIT_LOGIN_PASSWORD'],
    'redirect_uri': 'http://localhost:4300/sign_in', 'code_challenge': challenge, 'code_challenge_method': 'S256'},
    headers={'Origin': 'http://localhost:4300'})
grant = json.loads(body)
assert status == 200 and 'client_secret' not in grant and headers.get('Cache-Control') == 'no-store'
assert headers.get('Access-Control-Allow-Origin') == 'http://localhost:4300'
_, _, body = request('/o/token/', {'grant_type': 'authorization_code', 'code': grant['code'],
    'client_id': grant['client_id'], 'code_verifier': verifier, 'redirect_uri': 'http://localhost:4300/sign_in'}, form=True)
auth = {'Authorization': 'Bearer ' + json.loads(body)['access_token'], 'X-Client-ID': grant['client_id']}
print('PASS real password + PKCE exchange + browser CORS')
for path in ['/api/v1/auth/whoami/', '/api/v1/home-analytics/overview/', '/api/v1/home-analytics/business/',
             '/api/v1/home-analytics/intelligence/tokens/', '/api/v1/home-analytics/intelligence/rules/',
             '/api/v1/home-analytics/intelligence/runs/', '/api/v1/insights/']:
    status, _, body = request(path, headers=auth)
    assert status == 200
    json.loads(body)
    print('PASS authenticated API:', path)
_, _, body = request('/api/v1/home-analytics/intelligence/sources/', headers=auth)
sources = json.loads(body)['results']
docx = next(row for row in sources if row['filename'].endswith('.docx'))
status, headers, body = request('/api/v1/core/download/?' + urlencode({'file_id': docx['id'], 'inline': 'true'}), headers=auth)
assert status == 200 and body.startswith(b'PK')
print('PASS authenticated real DOCX source retrieval for in-app renderer')
print('All local HTTP smoke checks passed. Interactive browser rendering and live AI accuracy are separate checks.')
