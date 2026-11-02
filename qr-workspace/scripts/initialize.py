"""Create QR's local-only research configuration without displaying secrets."""
import json
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'user_data'


def main():
    for directory in ('logs', 'strategies', 'data', 'backtest_results'):
        (DATA / directory).mkdir(parents=True, exist_ok=True)
    target = DATA / 'config.json'
    if target.exists():
        print('Existing QR config retained.')
        return
    access_password = secrets.token_urlsafe(20)
    config = {
        '$schema': 'https://schema.freqtrade.io/schema.json',
        'bot_name': 'QR-development', 'strategy': 'SampleStrategy',
        'user_data_dir': '/freqtrade/user_data',
        'timeframe': '1h', 'max_open_trades': 1,
        'stake_currency': 'USDT', 'stake_amount': 100,
        'tradable_balance_ratio': 0.99, 'fiat_display_currency': 'USD',
        'dry_run': True, 'dry_run_wallet': 1000, 'fee': 0.001,
        'trading_mode': 'spot', 'margin_mode': '',
        'cancel_open_orders_on_exit': False,
        'unfilledtimeout': {'entry': 10, 'exit': 10, 'exit_timeout_count': 0, 'unit': 'minutes'},
        'entry_pricing': {'price_side': 'same', 'use_order_book': True, 'order_book_top': 1},
        'exit_pricing': {'price_side': 'same', 'use_order_book': True, 'order_book_top': 1},
        'exchange': {
            'name': 'binance', 'key': '', 'secret': '',
            'ccxt_config': {'enableRateLimit': True}, 'ccxt_async_config': {},
            'pair_whitelist': ['BTC/USDT'], 'pair_blacklist': [],
        },
        'pairlists': [{'method': 'StaticPairList'}],
        'telegram': {'enabled': False, 'token': '', 'chat_id': ''},
        'api_server': {
            'enabled': True, 'listen_ip_address': '0.0.0.0', 'listen_port': 8080,
            'verbosity': 'error', 'enable_openapi': True,
            'jwt_secret_key': secrets.token_urlsafe(48), 'ws_token': secrets.token_urlsafe(32),
            'CORS_origins': ['http://127.0.0.1:5173', 'http://localhost:5173'],
            'username': 'qr', 'password': access_password,
        },
        'initial_state': 'stopped', 'force_entry_enable': False,
        'internals': {'process_throttle_secs': 5},
    }
    target.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    (DATA / 'local-access.txt').write_text(
        f'Frontend: http://127.0.0.1:5173\nAPI: http://127.0.0.1:8090\nUsername: qr\nPassword: {access_password}\n',
        encoding='utf-8',
    )
    print('QR research configuration and local credentials created.')


if __name__ == '__main__':
    main()
