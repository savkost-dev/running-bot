# Схема цикла DoDick — где что лежит (19.09.2026)

## Сайт (живая, с кликами)
- Главная: `deploy/www/index.html` — карточка «Как это работает — один круг».
- Отдельная страница для пересылки: `deploy/www/cycle/index.html` → https://dodick.run/cycle/
- Превью ссылки (og:image): `deploy/www/cycle/og-cycle.png` → https://dodick.run/cycle/og-cycle.png
- Выкладка — вручную на Timeweb, папка `dodick.run` (не public_html).

## Видео (крутится прямо в Telegram)
Файлы сделаны в чате 19.09, хранить в `deploy/media/`:
- `dodick-cycle.mp4` — 1200×630, 14 с, без звука: для пересылки в чаты и каналы.
- `dodick-cycle-960x540.mp4` — для @BotFather → Edit Description Picture (экран «Что умеет этот бот?»).
- `dodick-cycle-640x360.mp4` — запасной размер для BotFather.
- `og-cycle.png` — статичная картинка 1200×630.

## Как пересобрать
`scripts/cycle_media/make_cycle_video.py` — кадры ролика (инструкция по склейке — в начале файла).
Шаги, подписи и порядок в ролике и на сайте заданы в трёх местах: оба html и этот скрипт — менять вместе.
