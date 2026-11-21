# Builders — Mech Royale

Бот на Python идёт к следующему безопасному кругу. Для наземного маршрута он читает геометрию зданий и рельеф из `/arena`, проверяет свободное место с учётом радиуса меха и выбирает направление обхода. Игровая точка входа — `example_bot.py`. Корневой `Dockerfile` использует закреплённый образ `python:3.13-slim` и стандартную библиотеку Python.

Коммит `2411871` хранит исходный Python-пример организатора. Из него взяты обёртка протокола `mechfight.py` и базовая логика в `example_bot.py`. Собственный алгоритм движения находится в `navigation.py`. Независимая проверка сравнивает поведение двух версий на геометрии официальной карты.

В официальной практике E01 с обходом препятствий получил 1,56, а E05 с наземным ускорением при контакте — 1,60. E07 подбирает снаряжение, собирает кубы, открывает ящики и управляет вторым слотом по его состоянию. На двух отложенных локальных сериях по 48 матчей E07 набрал 5,42 и 6,42. На этих же сериях E05 набрал 4,65 и 4,29. [Журнал экспериментов](PLAN.md) · [Результаты каждого матча](results/placement.csv)

Комплект получен 21 ноября 2026 года. Ниже указана контрольная сумма SHA256 скачанного архива. [Официальный стартовый набор Builders](https://www.joinbuilders.org/buildathon/tracks/builders/files/mechfight-kit.tar.gz)

```text
1758fe9125e193b268c6c1cffbf2c0ae96377214ab06d117412571c1523065ba
```

Правила игры и формат обмена сообщениями публикуют организаторы. [Страница трека](https://www.joinbuilders.org/buildathon/tracks/builders) · [Протокол mechfight/1](https://www.joinbuilders.org/buildathon/tracks/builders/files/protocol.md?inline=1)

Рабочие шаги и результаты храним в плане. Проект выпускается под лицензией MIT. [План и прогресс](PLAN.md) · [Условия лицензии](LICENSE)

## Получить инструменты проверки отдельно от проекта

Команды выполняются в одной оболочке Linux из корня репозитория. Для локального прогона понадобятся Python 3, `curl`, `tar` и `sha256sum`, для контейнерного — также Docker. Набор распаковывается во временную папку.

```sh
CHECK_KIT_DIR="$(mktemp -d)"
curl -fL https://www.joinbuilders.org/buildathon/tracks/builders/files/mechfight-kit.tar.gz \
  -o "$CHECK_KIT_DIR/mechfight-kit.tar.gz"
printf '1758fe9125e193b268c6c1cffbf2c0ae96377214ab06d117412571c1523065ba  %s\n' \
  "$CHECK_KIT_DIR/mechfight-kit.tar.gz" | sha256sum -c -
```

После успешной проверки контрольной суммы распакуйте набор.

```sh
tar -xzf "$CHECK_KIT_DIR/mechfight-kit.tar.gz" -C "$CHECK_KIT_DIR"
KIT_DIR="$CHECK_KIT_DIR/mechfight-kit"
```

## Сыграть локальную серию из 48 матчей

Стенд `SquadArena` проекта GT Goats играет 16-меховые матчи на карте из стартового набора. Адаптер загружает только симуляцию и её навигатор, сверяет файлы по SHA256 и получает действие нашего бота через `Bot.act`. Код участника GT Goats для управления мехом не используется. Соперники, физика и ресурсы в симуляторе приближённые. Задержки решений измеряются, пропуск просроченных действий проверяется отдельно официальной программой. [Исходники стенда и MIT-лицензия](https://github.com/atharvatripathi2026/gtgoats_mech_royale/tree/4b8012873dfd7ae43371fe0ee663104a6efebcc1)

Для воспроизведения на Linux выполните команды ниже после распаковки комплекта. Запускайте обе версии с одинаковым числом процессов. Параметр `--bot-dir` позволяет выбрать извлечённый через `git archive` коммит. Сравнивайте попарно одинаковые номера матчей. На скорость поиска пути влияет загрузка процессора, поэтому отдельные матчи при повторе могут отличаться.

```sh
ENGINE="$CHECK_KIT_DIR/engine"
mkdir -p "$ENGINE/training"
REV=4b8012873dfd7ae43371fe0ee663104a6efebcc1
for f in navigation.py training/arena.py training/squad16.py LICENSE; do
  curl -fsSL "https://raw.githubusercontent.com/atharvatripathi2026/gtgoats_mech_royale/$REV/$f" -o "$ENGINE/$f" || exit 1
done
python3 tools/local_matches.py --engine "$ENGINE" --kit "$KIT_DIR" --bot-dir . \
  --seeds 2001 --count 48 --workers 5 --json artifacts/series.json --csv artifacts/series.csv
```

В сравнении игровых тактик флаг `--fixed-planning` даёт обоим навигаторам одинаковый бюджет работы на каждом наблюдении. Время решения по-прежнему измеряется реальными часами. Флаг `--supplies` включает дополнительную модель грузов с расписанием и правилами кольца из комплекта организатора. Все живые мехи могут их захватывать. Соперники движутся по своим исходным маршрутам, точное распределение посадок сервера неизвестно. Параметры и ограничения каждой серии записываются в JSON. Отчёт также содержит время первого куба, заполнения второго слота и HP на 60-й секунде для доживших до неё мехов.

## Проверить бота

Для проверки движения прогоните три сценария — путь через здание, старт вплотную к стене и свободный участок. Затем запустите точку входа проекта на записи организатора и проверьте формат и скорость ответов.

```sh
python3 tests/check_routes.py --kit "$KIT_DIR"
python3 tests/check_mobility.py --kit "$KIT_DIR"
python3 tests/check_economy.py --kit "$KIT_DIR"
python3 -m unittest discover -s tests -p test_secondary.py
python3 "$KIT_DIR/check/check.py" --map-dir "$KIT_DIR/arena" \
  -- python3 -u ./example_bot.py
```

Чтобы проверить образ в условиях организатора, соберите его для `linux/amd64` и передайте в ту же программу проверки.

```sh
docker build --platform linux/amd64 -t mech-royale .
python3 "$KIT_DIR/check/check.py" --map-dir /arena -- \
  docker run --platform linux/amd64 --rm -i --network none --cpus 1 \
  --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "$KIT_DIR/arena:/arena:ro" mech-royale
```

Запускайте проверку контейнера Linux-версией Python, чтобы `--map-dir /arena` передал боту путь к смонтированной карте. `check.py` воспроизводит запись и проверяет протокол, время ответа и завершение. Игровой результат появляется после 48 практических матчей у организатора.

На каждый push GitHub Actions собирает образ на Linux x86_64, проверяет маршруты и трижды запускает контейнерную проверку организатора. [История проверок](https://github.com/Borovail/builders-mech-royale/actions/workflows/check-bot.yml)
