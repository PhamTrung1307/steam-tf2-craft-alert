# Build Windows App

## 1. Install Python

Install Python 3 from https://www.python.org/downloads/windows/ and enable **Add python.exe to PATH** during setup.

## 2. Build the exe

Double click:

```bat
build_windows.bat
```

The build installs dependencies from `requirements.txt` and runs PyInstaller.

## 3. Open the app

After the build finishes, open:

```text
dist/TF2CraftAlert.exe
```

## 4. Configure Telegram

Fill in:

```env
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Then click **Save Config**.

## 5. Test Telegram

Click **Test Telegram**. The app logs whether the test message was sent.

## 6. Start the bot

Click **Start Bot**. The monitor runs in the background and logs results in realtime.

## 7. Add or edit links

Click **Open Link.txt**, add one Steam Market URL per line, save the file, then start or restart the bot.
