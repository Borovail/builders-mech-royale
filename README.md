# Builders — Mech Royale

Бот на Python идёт к следующему безопасному кругу. Для наземного маршрута он читает геометрию зданий и рельеф из `/arena`, проверяет свободное место с учётом радиуса меха и выбирает направление обхода. Игровая точка входа — `example_bot.py`. Корневой `Dockerfile` использует закреплённый образ `python:3.13-slim` и стандартную библиотеку Python.

Коммит `2411871` хранит исходный Python-пример организатора. Из него взяты обёртка протокола `mechfight.py` и базовая логика в `example_bot.py`. Собственный алгоритм движения находится в `navigation.py`. Независимая проверка сравнивает поведение двух версий на геометрии официальной карты.

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

## Проверить бота

Для проверки движения прогоните три сценария — путь через здание, старт вплотную к стене и свободный участок. Затем запустите точку входа проекта на записи организатора и проверьте формат и скорость ответов.

```sh
python3 tests/check_routes.py --kit "$KIT_DIR"
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
