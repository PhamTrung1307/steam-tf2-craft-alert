# Deploy Render Free

1. Tao repo GitHub ten:

```text
steam-tf2-craft-alert
```

2. Push code len GitHub:

```bash
git add .
git commit -m "prepare render deployment"
git branch -M main
git push -u origin main
```

3. Vao:

```text
https://render.com
```

4. Sign in bang GitHub.

5. Bam **New +** -> **Web Service**.

6. Connect repo:

```text
steam-tf2-craft-alert
```

7. Chon **Free** instance.

8. Build command:

```bash
pip install -r requirements.txt
```

9. Start command:

```bash
python main.py
```

10. Add Environment Variables:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
CHECK_INTERVAL=60
REQUEST_DELAY=5
```

11. Bam **Deploy**.

12. Mo **Logs** de xem:

```text
Monitor started
TF2 Craft Alert Running
```

13. Muon them/sua link:

```bash
# sua Link.txt truoc
git add .
git commit -m "update links"
git push
```

Render se tu redeploy sau khi push neu auto deploy dang bat.
