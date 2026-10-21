# radio-record-server

Приём аудиопотока с ESP32 (радиотюнер/ADC → I2S) и запись его в сегментированный архив.

Стек выбран под сценарий «только запись»: минимальные накладные расходы на кадр,
предсказуемая задержка, никаких зависимостей в hot-path.

| Слой | Технология | Роль |
| --- | --- | --- |
| Транспорт | `asyncio.start_server` (stdlib) | TCP-приём кадров, backpressure |
| Формат на проводе | 43-байтный бинарный заголовок + PCM s16le | посильно прошивке ESP32 |
| Запись | `WavWriter` (stdlib `struct`) | потоковая запись сегментов `.part` → `.wav` |
| Пост-обработка | PyAV (`av`), опционально | FLAC / Opus, только после закрытия сегмента |
| GUI | FastAPI + uvicorn + SSE | журнал и состояние подключений, read-only |
| Управление | CLI (`argparse`) | без HTTP-слоя в аудиопути |

WebSocket/HTTP-инжест, RTSP и Icecast здесь не нужны: они добавляют оверхед и код
на стороне микроконтроллера, ничего не давая архиву. Если позже понадобится живое
прослушивание — смотрите в сторону Icecast как отдельного сервиса.

Дашборд (FastAPI) работает отдельным портом и только читает состояние: аудио через
него не проходит, приём кадров от него не зависит.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[transcode]"
```

Без `[transcode]` сервер пишет WAV и не требует PyAV; FastAPI/uvicorn нужны всегда,
потому что на них работает дашборд (`--no-dashboard` его отключает).

## Запуск

```powershell
.\.venv\Scripts\python.exe -m radio_record --port 9100 --output recordings --segment-minutes 15
# с перекодированием в FLAC (WAV удаляется после успешного энкода)
.\.venv\Scripts\python.exe -m radio_record --port 9100 --transcode flac
# дашборд откроется в браузере: http://127.0.0.1:8080/
.\.venv\Scripts\python.exe -m radio_record --open-dashboard
```

| Флаг | По умолчанию | Смысл |
| --- | --- | --- |
| `--host` / `--port` | `0.0.0.0` / `9100` | адрес прослушивания |
| `--output` | `recordings` | корень архива |
| `--sample-rate` / `--channels` | `16000` / `1` | обязаны совпадать с прошивкой, иначе кадры отклоняются |
| `--segment-minutes` | `15` | длина сегмента (считается по сэмплам, не по часам) |
| `--max-segment-mb` | `1024` | аварийная ротация по размеру |
| `--transcode` | выкл. | `flac` или `opus` |
| `--keep-wav` | выкл. | оставить WAV после перекодирования |
| `--no-pad-gaps` | выкл. | не добивать пропуски тишиной |
| `--dashboard-host` / `--dashboard-port` | `127.0.0.1` / `8080` | адрес дашборда |
| `--open-dashboard` | выкл. | открыть дашборд в браузере при старте |
| `--no-dashboard` | выкл. | headless-режим, только приём |

## Дашборд

`http://127.0.0.1:8080/` — одна страница без сборки и внешних CDN:

- **Активные подключения**: устройство, адрес, состояние (`поток идёт` / `нет данных`),
  скорость, кадры, получено байт, длительность аудио, накопленные пропуски, простой,
  текущий сегмент с прогрессом до ротации, глубина очереди (индикатор backpressure)
  и число задач пост-обработки.
- **Последние сессии**: до 20 завершённых подключений с причиной завершения
  (`поток завершён`, `связь потеряна`, `нет данных`, `ошибка протокола: …`).
- **Журнал**: живой поток логов с фильтром по уровню, паузой, автоскроллом и очисткой.

Обновление идёт через SSE: сервер отдаёт `event: status` раз в секунду и `event: log`
по мере появления записей, страница подключается заново автоматически после разрыва.

Эндпоинты (все только чтение, JSON):

| Путь | Назначение |
| --- | --- |
| `GET /` | страница дашборда |
| `GET /healthz` | `{"status": "ok"}` для проб |
| `GET /api/status` | снимок состояния: сервер + устройства + недавние сессии |
| `GET /api/logs?after=<seq>&limit=<n>` | журнал из кольцевого буфера |
| `GET /api/events` | SSE: `status` и `log` |

По умолчанию дашборд слушает только loopback: он показывает идентификаторы
устройств и логи, а аутентификации в нём нет. Для доступа с другой машины
осознанно укажите `--dashboard-host 0.0.0.0` и закройте порт файрволом/прокси
с аутентификацией.

В журнал попадают записи уровня INFO и выше от всех логгеров, включая `uvicorn`;
логгер `asyncio` не засоряет его обрывами соединений — на Windows закрытие
вкладки/сокета иначе даёт `ConnectionResetError` в транспорте.

## За Traefik

У сервера два порта, и это два разных типа роутинга: HTTP (дашборд) и сырой TCP
(аудио). Аудио через HTTP не идёт, поэтому его нельзя повесить на HTTP-роутер.

Коротко:

- **Дашборд** работает через Traefik без изменений кода. Обязательное условие —
  слушать не loopback: `--dashboard-host 0.0.0.0` (или адрес docker-сети).
- **Аудио** проходит через TCP-роутер без изменений: протокол бинарный,
  прокси его не разбирает и не буферизует.
- **Не работает «из коробки»**: реальный IP устройства (у TCP нет
  `X-Forwarded-For`, а PROXY protocol Traefik бэкенду не отправляет) и TLS на
  аудиопорту.

### Статическая конфигурация Traefik

```yaml
entryPoints:
  web:
    address: ":80"
  websecure:
    address: ":443"
  ingest:
    address: ":9100"          # отдельный порт только под аудио
```

### docker-compose

Образ собирается из [Dockerfile](Dockerfile); в контейнере дашборд по умолчанию
слушает `0.0.0.0` (иначе Traefik до него не достучится), архив — в `/data/recordings`.

```powershell
docker build -t radio-record .
```

### В общей сети с уже работающим Traefik

Готовый [docker-compose.yml](docker-compose.yml) рассчитан именно на такой сервер:
контейнер только подключается к существующей сети `frontend`, а маршрут дашборда
объявлен лейблами контейнера. Поэтому не нужно править ни `traefik.yml`, ни
`dynamic_conf.yml` — существующие роутеры и сервисы не затрагиваются. Реальное имя
сети — `<compose-проект>_frontend`, поэтому оно берётся из `TRAEFIK_NETWORK`
(по умолчанию `traefik_frontend`).

- хост в лейблах — `radio-record.mldl.ru`; замените на своё имя и заведите под него
  DNS-запись, иначе Let's Encrypt не выпустит сертификат;
- на хосте публикуется только аудиопорт `9100`, статическая конфигурация Traefik
  не меняется;
- дашборд наружу не публикуется: Traefik ходит в него по имени контейнера на
  `8080` ([`--dashboard-host 0.0.0.0`](Dockerfile) в образе уже стоит);
- архив лежит в именованном томе `radio-record-data` (владелец — UID `10001`),
  так что bind-mount на каталог хоста потребует `chown -R 10001:10001`.

ESP32 присылает аудио на `radio-record.mldl.ru:9100` — порт опубликован напрямую,
TLS для аудиопотока не нужен.

Учтите: хост в лейблах должен быть уникальным среди всех роутеров Traefik. Если
маршруты по конвенции живут в `dynamic_conf.yml`, добавьте туда новые роутер и
сервис, не меняя существующие:

```yaml
    to-radio-record:
      rule: "Host(`radio-record.mldl.ru`)"
      entryPoints:
        - websecure
      service: radio-record
      tls:
        certResolver: letsencrypt
```

```yaml
    radio-record:
      loadBalancer:
        servers:
          - url: "http://radio-record:8080"
        passHostHeader: true
```

В этом случае лейблы `traefik.http.*` из compose нужно убрать: два роутера на один
`Host` дадут одинаковый приоритет и недетерминированный выбор.

Если нужно, чтобы аудио тоже шло через Traefik, понадобится отдельный
TCP-entrypoint (см. выше), а публикацию `9100` из compose придётся убрать.

### Пример с аудио через Traefik

Если у проекта отдельный Traefik или `ingest`-entrypoint уже добавлен, звук можно
провести через Traefik вместе с дашбордом:

```yaml
services:
  radio-record:
    build: .
    command: >
      python -m radio_record
      --port 9100 --dashboard-port 8080 --dashboard-host 0.0.0.0
      --output /data/recordings --segment-minutes 15
    volumes:
      - /srv/radio:/data/recordings
    labels:
      # --- аудио: отдельный TCP-entrypoint, catch-all ---
      - traefik.enable=true
      - traefik.tcp.routers.radio-ingest.rule=HostSNI(`*`)
      - traefik.tcp.routers.radio-ingest.entrypoints=ingest
      - traefik.tcp.routers.radio-ingest.service=radio-ingest
      - traefik.tcp.services.radio-ingest.loadbalancer.server.port=9100
      # --- дашборд: обычный HTTP-роутер ---
      - traefik.http.routers.radio-ui.rule=Host(`radio.example.com`)
      - traefik.http.routers.radio-ui.entrypoints=websecure
      - traefik.http.routers.radio-ui.tls.certresolver=le
      - traefik.http.routers.radio-ui.service=radio-ui
      - traefik.http.services.radio-ui.loadbalancer.server.port=8080
      - traefik.http.services.radio-ui.loadbalancer.healthcheck.path=/healthz
      # --- аутентификация дашборда (см. ниже) ---
      - traefik.http.routers.radio-ui.middlewares=radio-ui-auth
      - traefik.http.middlewares.radio-ui-auth.basicauth.users=admin:$$apr1$$...
```

В варианте с лейблами ESP32 подключается на `radio.example.com:9100`, а дашборд
открыт на `https://radio.example.com/`.

Почему именно так:

- `HostSNI(\`*\`)` — единственный способ поймать весь non-TLS TCP, поэтому
  entrypoint `ingest` должен быть выделенным: catch-all перехватит всё на этом
  порту. TCP-роутеры на общем entrypoint обрабатываются раньше HTTP.
- SSL для аудио не обязателен: трафик и так не HTTP, а TLS на ESP32 стоит RAM.
  Если нужен — `traefik.tcp.routers.radio-ingest.tls.certresolver=le` плюс
  `HostSNI(\`radio.example.com\`)` вместо `*`, и прошивка должна ходить через
  `WiFiClientSecure` с SNI.
- SSE через Traefik проходит без настройки: `respondingTimeouts.writeTimeout`
  по умолчанию `0`, `readTimeout` (60 с) относится к чтению запроса, а
  `idleTimeout` (180 с) — к простаивающему keep-alive; здесь данные идут каждую
  секунду. Ответы Traefik не буферизует, заголовок `X-Accel-Buffering` в ответе
  сервера — страховка для nginx и Traefik не мешает.
- Префикс пути тоже поддержан: `PathPrefix(\`/radio\`)` + `StripPrefix`, страница
  использует относительные URL API (`/radio/api/status`). Роутинг по Host всё же
  предпочтительнее — меньше сюрпризов с абсолютными ссылками.
- Одна реплика: TCP-сессия закрепляется за репликой, но дашборд показывает только
  сессии своего процесса. Для нескольких реплик нужен sticky-роутинг по устройству
  или отдельный агрегатор состояния.

### Что требует изменений, если понадобится

| Задача | Что делать |
| --- | --- |
| реальный IP ESP32 в логах и дашборде | опубликовать `9100` напрямую, либо добавить в сервер разбор PROXY protocol v1 и включить `proxyProtocol.trustedIPs` у entrypoint |
| дашборд в интернете | `basicAuth`/`IPAllowList` в Traefik: своей аутентификации у дашборда нет |
| несколько реплик | sticky по `device_id` + внешняя агрегация статусов |

## Протокол

Кадр = заголовок (43 байта, little endian) + PCM. Один кадр — один блок
непрерывных сэмплов, обычно 20 мс.

| Смещение | Размер | Поле | Значение |
| --- | --- | --- | --- |
| 0 | 4 | `magic` | `"RDIO"` |
| 4 | 1 | `version` | `1` |
| 5 | 1 | `codec` | `1` = PCM signed 16-bit LE |
| 6 | 16 | `device_id` | ASCII, дополнен `\0` |
| 22 | 4 | `seq` | счётчик кадров соединения, u32 |
| 26 | 8 | `first_sample` | абсолютный номер первого сэмпла, u64 |
| 34 | 4 | `payload_len` | размер payload, 1…65536 |
| 38 | 4 | `sample_rate` | сэмплов/с на канал |
| 42 | 1 | `channels` | 1 или 2 |
| 43 | N | payload | PCM s16le |

Порядок байт little endian выбран сознательно: ESP32 little-endian, поэтому
прошивка заполняет packed-структуру и пишет её без байтсвопа.

`first_sample` — ключевое поле: сервер строит таймлайн сегментов и находит
пропуски по номерам сэмплов, а не по настенным часам. Дрейф таймера ESP32
не растягивает и не сжимает запись.

## Прошивка ESP32

```cpp
#include <WiFi.h>

static const char    *DEVICE_ID   = "esp32-a";
static const uint32_t SAMPLE_RATE = 16000;

#pragma pack(push, 1)
struct FrameHeader {
  char     magic[4];      // "RDIO"
  uint8_t  version;       // 1
  uint8_t  codec;         // 1 = PCM s16le
  char     device_id[16];
  uint32_t seq;
  uint64_t first_sample;
  uint32_t payload_len;
  uint32_t sample_rate;
  uint8_t  channels;
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader) == 43, "must match the server format");

WiFiClient client;
uint32_t   seq         = 0;
uint64_t   sampleIndex = 0;

void streamFrame(const int16_t *pcm, size_t samples) {
  FrameHeader h{};
  memcpy(h.magic, "RDIO", 4);
  h.version     = 1;
  h.codec       = 1;
  strncpy(h.device_id, DEVICE_ID, sizeof h.device_id);
  h.seq         = seq++;
  h.first_sample = sampleIndex;
  h.payload_len  = samples * sizeof(int16_t);
  h.sample_rate  = SAMPLE_RATE;
  h.channels     = 1;

  client.write(reinterpret_cast<const uint8_t *>(&h), sizeof h);
  client.write(reinterpret_cast<const uint8_t *>(pcm), h.payload_len);
  sampleIndex += samples;
}

// в задаче чтения I2S/ADC:
//   int16_t buf[320];                 // 20 мс при 16 кГц
//   size_t n = readRadioSamples(buf, 320);
//   streamFrame(buf, n);
```

Практические замечания:

- `client.setNoDelay(true)` — иначе Nagle добавляет задержку и склеивает кадры.
- `sampleIndex` должен оставаться монотонным между реконнектами: сервер
  воспринимает его как абсолютную шкалу времени и продолжит сегмент без разрыва.
  После перезагрузки платы счётчик начинается с нуля — сервер это обнаруживает
  и открывает новый сегмент.
- `client.write()` может блокироваться: это штатный backpressure, когда сервер
  не успевает писать на диск. Терять кадры хуже, чем подождать.
- Частоту дискретизации лучше брать из реального тактирования I2S/ADC: если
  кварц уходит, запись будет «плыть». Компенсация дрейфа — на стороне сервера
  (ресемплинг), в этот каркас не входит.

## Проверка без железа

```powershell
# терминал 1
.\.venv\Scripts\python.exe -m radio_record --port 9100 --segment-minutes 0.05 --transcode flac

# терминал 2: 5 секунд эфира на скорости x5, с пропуском 0.5 с на второй секунде
.\.venv\Scripts\python.exe tools\fake_esp32.py --port 9100 --seconds 5 --speed 5 --gap-at 2 --gap-seconds 0.5
```

Результат — `recordings/esp32-sim/esp32-sim-<UTC>.flac`, по файлу на сегмент,
и живая карточка устройства в дашборде, пока клиент держит соединение.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

Тесты дашборда поднимают настоящий uvicorn на эфемерном порту и ходят в него по
HTTP/SSE без дополнительных клиентских зависимостей.

## Раскладка архива

```
recordings/
  esp32-a/
    esp32-a-20261021T141201Z.wav      # или .flac / .opus
    esp32-a-20261021T141203Z.flac
```

Имя = устройство + UTC-время начала сегмента. Пока сегмент пишется, файл лежит
с расширением `.part`, поэтому потребитель архива никогда не увидит недописанный
WAV.

## Поведение, на которое можно рассчитывать

- **Пропуски.** Если `first_sample` прыгает вперёд, сервер добивает разрыв тишиной
  (до `max_gap_seconds = 5`), чтобы длительность записи соответствовала эфиру.
  Разрыв больше лимита обрезается, а факт фиксируется в логе.
- **Backpressure вместо потерь.** На устройство выделена очередь кадров
  (`queue_frames = 512`, ~10 с при 20 мс кадрах); при переполнении чтение из
  сокета приостанавливается, окно TCP закрывается, ESP32 ждёт.
- **Реконнект** закрывает текущий сегмент и открывает новый.
- **Повторное подключение того же `device_id`** вытесняет старую сессию, чтобы
  не писать два потока в один каталог.
- **Молчание дольше 30 с** (`idle_timeout`) закрывает сессию и финализирует файл.
- **Пост-обработка** (FLAC/Opus) идёт отдельной задачей и не тормозит приём.
- **Мусор на входе** (неверный magic, версия, кодек, частота, каналы) отклоняется
  с записью в лог, соединение закрывается, файлов не появляется.
- **Дашборд** не влияет на приём: если браузер отвалился или uvicorn упал,
  аудио продолжает писаться, а закрытие вкладки не оставляет ошибок в журнале.

## Ограничения

- Один поток с устройства; мультиплексирования дорожек в одном соединении нет.
- Только PCM s16le. ADPCM/Opus потребуют добавить кодек в заголовок и декодер
  перед записью.
- Нет аутентификации ни у ingest-порта, ни у дашборда: рассчитано на доверенную
  локальную сеть. Для выхода в интернет добавьте PSK-обмен или mTLS на уровне
  транспорта, а дашборд держите за прокси с аутентификацией.
- Ресемплинг и оценка фактической частоты не реализованы — см. замечание про
  кварц выше.
- Дашборд read-only: из интерфейса нельзя перезапустить сессию, удалить запись
  или изменить настройки — это осознанно, чтобы UI не мог испортить архив.
