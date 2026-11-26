# Система управления фракцией

Discord-бот + веб-панель с входом через Discord. Ранги задаются вами (название + роль
Discord + порог очков), у каждого участника своё личное дело с отметками, которые
повышают, понижают или не влияют на его счёт и ранг.

## Возможности

**Отделы и иерархии**
- Отдел — самостоятельная лестница ступеней: у каждого отдела свои названия рангов,
  уровни, пороги очков и роли Discord.
- Участник двигается **только внутри своего отдела**: смена отдела переносит его на
  ступень, которую в новой лестнице даёт его счёт, а не сохраняет старый ранг.
- Офицер двигает участника на ступень выше/ниже — командой `/promote`, `/demote` или
  кнопками в личном деле. Движение упирается в края лестницы и не перескакивает в
  чужие отделы.
- Новый отдел можно создать пустым или скопировать лестницу из существующего.
- Отдел «по умолчанию» получают участники без отдела; он же используется для авторанга.

**Ранги**
- Своё название, цвет, уровень в иерархии и порог очков для каждой ступени.
- К ступени привязывается роль Discord — при смене ранга бот сам выдаёт новую
  роль и снимает старые роли рангов.
- Авторанг: после каждой отметки ранг пересчитывается по сумме очков внутри отдела
  участника (можно выключить и назначать вручную).

**Личное дело участника**
- Отдельная страница на каждого: аватар, позывной, ранг, счёт, прогресс до следующего ранга.
- История всех отметок: кто выдал, когда, комментарий, влияние на счёт.
- Отметки бывают положительные (`+5`), отрицательные (`-10`) и нейтральные (`0`) —
  нейтральные фиксируются в деле, но не влияют на ранг.
- Отзыв отметки возвращает счёт и ранг назад.
- Шаблоны частых отметок для быстрой выдачи.

**Вход**
- Discord OAuth2 (`identify` + `guilds.members.read`). Пароли не используются.
- Подписанная HttpOnly cookie, CSRF-токен на каждой форме, срок жизни сессии 12 часов.
- Офицеры определяются по списку ID ролей или по `ADMIN_IDS`.

**Discord-команды**
| Команда | Кто | Что делает |
|---|---|---|
| `/panel` | все | личная ссылка на панель |
| `/file [участник]` | все | личное дело прямо в Discord |
| `/roster` | все | состав фракции |
| `/rank list` | все | лестница рангов |
| `/rank create/edit/delete` | офицеры | управление рангами и ролями |
| `/rank assign` | офицеры | выдать ранг вручную |
| `/department list` | все | отделы и их ступени |
| `/department create/edit/delete` | офицеры | управление отделами и их лестницами |
| `/department assign` | офицеры | зачислить участника в отдел |
| `/promote` / `/demote` | офицеры | ступень выше / ниже внутри отдела |
| `/mark give` | офицеры | выдать отметку (`delta` = влияние) |
| `/mark revoke` | офицеры | отозвать отметку по номеру |
| `/sync` | офицеры | обновить состав и пересинхронизировать роли |
| `/setup` | офицеры | привязать сервер к системе |

## Установка

1. Создайте приложение: <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → Reset Token → скопируйте токен.
3. **OAuth2** → скопируйте **Client ID** и **Client Secret**.
4. **OAuth2 → Redirects** добавьте `https://ВАШ-ДОМЕН/callback`
   (для локального теста — `http://localhost:8000/callback`).
5. **Bot → Privileged Gateway Intents** включите **Server Members Intent**.
6. Пригласите бота с правами **Manage Roles** (и `applications.commands`).

```bash
cp .env.example .env
# заполните DISCORD_TOKEN, CLIENT_ID, CLIENT_SECRET, BASE_URL
./run.sh
```

При первом запуске создаётся `.env` (если его нет) и база `data/faction.db`
со стартовой лестницей рангов: Рекрут → Боец → Ветеран → Офицер → Командир.

### Важно про роли
Роль бота на сервере должна стоять **выше** всех ролей рангов, иначе Discord
запретит их выдачу (бот покажет предупреждение в ответе на команду).

### Привязка сервера
После запуска выполните `/setup` на своём сервере — система запомнит guild id и
сможет читать роли участников при входе через OAuth.

## Настройки (`.env`)

| Переменная | Обязательно | Назначение |
|---|---|---|
| `DISCORD_TOKEN` | да | токен бота |
| `CLIENT_ID` | да | OAuth2 client id |
| `CLIENT_SECRET` | да | OAuth2 client secret |
| `BASE_URL` | да | публичный адрес панели, redirect = `BASE_URL/callback` |
| `PORT` / `HOST` | нет | адрес веб-сервера (по умолчанию `8000` / `0.0.0.0`) |
| `FACTION_NAME` | нет | название по умолчанию |
| `COMMAND_GUILD_ID` | нет | guild для мгновенной регистрации слэш-команд |
| `OFFICER_ROLE_IDS` | нет | ID ролей офицеров через запятую |
| `ADMIN_IDS` | нет | Discord ID полных администраторов |
| `SESSION_SECRET` | нет | ключ cookie (иначе генерируется в `data/session.key`) |
| `DB_PATH` | нет | путь к SQLite |

## Структура

```
app.py          запуск бота и веб-панели в одном процессе
bot.py          слэш-команды, выдача ролей, кэш состава
main.py         FastAPI: OAuth, личные дела, отделы, ранги, настройки
core.py         бизнес-логика: отметки, авторанг, шаг по иерархии, синхронизация ролей
db.py           SQLite: отделы, ранги, участники, отметки, журнал + миграции схемы
auth.py         Discord OAuth2 и подписанные сессии
config.py       конфигурация из .env
templates/      Jinja2-шаблоны панели
static/app.css  стили
tests/          офлайн-тесты панели, логики и OAuth-хелперов
Dockerfile      образ бота+панели
docker-compose.yml   запуск с томом ./data
docker-entrypoint.sh проверка прав на каталог данных
```

## Тесты

```bash
.venv/bin/python tests/test_web.py        # HTTP-панель: права, CSRF, отметки, отделы, шаг по лестнице
.venv/bin/python tests/test_core.py       # авторанг, отзыв, синхронизация ролей, OAuth
.venv/bin/python tests/test_roles.py      # выдача ролей Discord на заглушках
.venv/bin/python tests/test_commands.py   # слэш-команды, включая /department и /promote
.venv/bin/python tests/test_migration.py  # миграция старой базы на отделы без потерь
```

Каждый набор работает со своей базой в `data/test_*.db` и не трогает рабочую
`data/faction.db`.

## Docker

Бот и панель живут в одном контейнере (один процесс, как и при `run.sh`).

```bash
cp .env.example .env    # DISCORD_TOKEN, CLIENT_ID, CLIENT_SECRET, BASE_URL
mkdir -p data           # сюда монтируется база и session.key
docker compose up -d --build
docker compose logs -f
```

В `BASE_URL` укажите публичный адрес панели — redirect URI в Discord
остаётся `BASE_URL/callback`.

Что важно знать про образ:

- Порт внутри контейнера всегда `8000`; наружу выставляется `PANEL_PORT`
  (по умолчанию тоже `8000`). `PORT` из `.env` в контейнере игнорируется,
  чтобы маппинг портов не разъезжался.
- `./data` монтируется в `/app/data` — там SQLite-база и ключ сессии.
  Это единственный изменяемый каталог (корень ФС только для чтения).
- Контейнер работает не от root. Если `./data` принадлежит другому uid,
  задайте его: `APP_UID=$(id -u) APP_GID=$(id -g) docker compose up -d --build`.
- Остановка штатная: `docker stop` шлёт `SIGTERM`, `app.py` сам закрывает Discord
  и затем uvicorn (`stop_grace_period: 30s`).
- Healthcheck бьёт в `/health`; `docker compose ps` покажет `healthy`.
- Если каталог `./data` недоступен для записи, контейнер упадёт сразу с понятным
  сообщением вместо трейсбека SQLite (`docker-entrypoint.sh`).

Без compose:

```bash
docker build -t faction-bot:latest .
docker run -d --name faction-bot --restart unless-stopped \
  --env-file .env -e PORT=8000 -e DB_PATH=/app/data/faction.db \
  -p 8000:8000 -v "$PWD/data:/app/data" faction-bot:latest
```

## systemd

```bash
cp faction-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now faction-bot
```

## За reverse proxy

Панель должна быть доступна по HTTPS: Discord требует https для redirect URI,
кроме `localhost`. Пример nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Cookie сессии ставится с `SameSite=Lax` без `Secure`, чтобы работать и по http на
локальной машине. За HTTPS-прокси включите `Secure` в `main.py` (две строки
`set_cookie`), если панель открыта в интернет.
