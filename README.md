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
| Управление | CLI (`argparse`) | без HTTP-слоя в аудиопути |

WebSocket/HTTP-инжест, RTSP и Icecast здесь не нужны: они добавляют оверхед и код
на стороне микроконтроллера, ничего не давая архиву. Если позже понадобится живое
прослушивание — смотрите в сторону Icecast как отдельного сервиса.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[transcode]"
```

Без `[transcode]` сервер пишет WAV и не требует тяжёлых зависимостей.

## Запуск

```powershell
.\.venv\Scripts\python.exe -m radio_record --port 9100 --output recordings --segment-minutes 15
# с перекодированием в FLAC (WAV удаляется после успешного энкода)
.\.venv\Scripts\python.exe -m radio_record --port 9100 --transcode flac
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

Результат — `recordings/esp32-sim/esp32-sim-<UTC>.flac`, по файлу на сегмент.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

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

## Ограничения

- Один поток с устройства; мультиплексирования дорожек в одном соединении нет.
- Только PCM s16le. ADPCM/Opus потребуют добавить кодек в заголовок и декодер
  перед записью.
- Нет аутентификации: рассчитано на доверенную локальную сеть. Для выхода в
  интернет добавьте PSK-обмен или mTLS на уровне транспорта.
- Ресемплинг и оценка фактической частоты не реализованы — см. замечание про
  кварц выше.
