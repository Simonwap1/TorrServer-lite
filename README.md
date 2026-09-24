# TorrServer Lite (iPad UI)

Упрощённый веб-интерфейс для iPad поверх [TorrServer / MatriX](https://github.com/YouROK/TorrServer) (**YouROK**).

- UI: порт `8080` (`ipad/gateway.py` + `ipad/index.html`)
- TorrServer: порт `8092`
- Запуск на Windows: `films.pyw` или `start.bat`

Репозиторий: https://github.com/Simonwap1/TorrServer-lite

## Благодарность

TorrServer создан **YouROK**: https://github.com/YouROK/TorrServer

## Что нужно

- Python 3
- `TorrServer-windows-amd64.exe` (есть в репозитории)
- ffmpeg (ставится автоматически в `tools/ffmpeg` при первом запуске)

## Что не в git

Личные данные и тяжёлый мусор не выкладываются: торренты, `hls_cache`, базы (`config.db`), логи, Prowlarr, `tools/ffmpeg`.
