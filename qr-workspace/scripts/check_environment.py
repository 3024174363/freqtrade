"""Read-only runtime checks; never display local credentials."""
import json
from pathlib import Path

import freqtrade
import requests
from freqtrade.configuration import Configuration


def main():
    path = Path('/freqtrade/user_data/config.json')
    config = json.loads(path.read_text())
    Configuration.from_files([str(path)])
    assert str(Path(freqtrade.__file__)).startswith('/workspace/freqtrade/'), freqtrade.__file__
    assert config['dry_run'] is True
    assert config['fee'] == 0.001
    assert not config['exchange'].get('key') and not config['exchange'].get('secret')
    api = config['api_server']
    session = requests.Session()
    url = 'http://127.0.0.1:8080/api/v1/'
    response = session.get(url + 'ping', timeout=10)
    response.raise_for_status()
    response = session.post(url + 'token/login', auth=(api['username'], api['password']), timeout=10)
    response.raise_for_status()
    session.headers['Authorization'] = 'Bearer ' + response.json()['access_token']
    for endpoint in ('version', 'strategies', 'show_config'):
        response = session.get(url + endpoint, timeout=20)
        response.raise_for_status()
        data = response.json()
        if endpoint == 'show_config':
            data = {k: data.get(k) for k in ('dry_run', 'runmode', 'trading_mode', 'timeframe')}
        print(endpoint + ':', json.dumps(data, ensure_ascii=False))
    base = 'http://127.0.0.1:8080'
    for endpoint in ('/', '/trade', '/assets/index.js', '/fallback_file.html', '/ui_version'):
        response = session.get(base + endpoint, timeout=10)
        assert response.status_code == 404, (endpoint, response.status_code)
        assert response.json() == {'detail': 'Not Found'}, endpoint
    response = session.get(base + '/docs', timeout=10)
    response.raise_for_status()
    assert 'swagger-ui' in response.text
    print('PASS: local source, research config, 0.1% fee, health, authentication, API-only backend, docs.')


if __name__ == '__main__':
    main()
