# TorrServer Lite (iPad UI)

Упрощённый веб-интерфейс для iPad поверх [TorrServer / MatriX](https://github.com/YouROK/TorrServer) (**YouROK**).

- UI: порт `8080` (`ipad/gateway.py` + `ipad/index.html`)
- TorrServer: порт `8092`
- Запуск на Windows: `films.pyw` или `start.bat`

## Благодарность

TorrServer создан **YouROK**: https://github.com/YouROK/TorrServer

## Что нужно

- Python 3
- TorrServer (`TorrServer-windows-amd64.exe` рядом с `films.pyw`)
- ffmpeg (ставится автоматически в `tools/ffmpeg` при первом запуске)

## Как выложить на GitHub

1. Установите [Git](https://git-scm.com/download/win) и войдите в [GitHub](https://github.com).
2. На GitHub: **New repository** → имя, например `torrserver-lite` → Create (без README, если уже есть локально).
3. В PowerShell в папке проекта:

```powershell
cd C:\vid
git init -b main
git add .
git status
git commit -m "TorrServer Lite for iPad"
git remote add origin https://github.com/Simonwap1/torrserver-lite.git
git push -u origin main
```

В `.gitignore` уже исключены торренты, exe, кэш HLS, Prowlarr и личные данные — в репозиторий уйдёт в основном код.

Репозиторий: https://github.com/Simonwap1/torrserver-lite
