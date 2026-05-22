# steam-tf2-craft-alert

Bot monitor item TF2 tren Steam Community Market va gui Telegram khi item craftable.

Project nay san sang deploy len Render Free bang Web Service. Khong dung Docker, khong hardcode token, khong commit `.env`.

## Production tren Render

Render chay:

```Procfile
web: python main.py
```

`python main.py` tu start 2 thread:

- Thread monitor Steam.
- Thread health web server.

Health server:

- Host: `0.0.0.0`
- Port: `os.getenv("PORT", 8000)`
- Endpoint: `/`
- Response: `TF2 Craft Alert Running`

## Environment Variables

Nhap tren Render:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
CHECK_INTERVAL=60
REQUEST_DELAY=5
```

Optional:

```env
REQUEST_TIMEOUT=20
```

`.env` chi dung local va bi ignore boi Git.

## Chay local

```bash
pip install -r requirements.txt
python main.py
```

Lenh ho tro:

```bash
python main.py --once
python main.py --test-telegram
python main.py --debug-single URL
```

## Link.txt

Moi dong la mot Steam Market URL. Dong trong va dong bat dau bang `#` se bi bo qua.

Neu thieu hoac rong `Link.txt`, app log loi de hieu va tiep tuc chay vong sau.

## Log

Log binh thuong:

```text
[TIME] ITEM_NAME | CRAFTABLE
[TIME] ITEM_NAME | NOT_CRAFTABLE
```

HTML debug chi in khi dung `--debug` hoac `--debug-single`.

## Deploy

Xem [DEPLOY_RENDER.md](DEPLOY_RENDER.md).
