# QuestSpeak — Озвучка квестов World of Warcraft (русский)

**Версия:** 1.9 (Lua) + 1.0 (daemon) · **Платформа:** Linux + WoW (Proton/Wine) ·
**TTS:** [piper-tts](https://github.com/rhasspy/piper) с русским голосом
`ru_RU-irina-medium`.

Читает в голос текст квестов и диалоги NPC в реальном времени. Без OCR,
без скриншотов: Lua-аддон вытаскивает текст прямо из WoW API, а внешний
Python-демон озвучивает его нейросетевым голосом.

---

## Архитектура

```
┌────────────────────────────────────────────────────────────────────┐
│ WoW (Proton/Wine)                                                  │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  QuestSpeak.lua                                           │      │
│  │                                                            │     │
│  │  QUEST_DETAIL ──┐                                          │     │
│  │  QUEST_PROGRESS ┤──► GetTitleText() + GetQuestText()       │     │
│  │  QUEST_COMPLETE ┤   + GetRewardText() + GetObjectiveText() │     │
│  │  GOSSIP_SHOW    ─┘   (без OCR, чистый API)                │      │
│  │                       │                                    │     │
│  │                       ▼                                    │     │
│  │     dataFrame.text = "§QS§текст квеста§/QS§"               │     │
│  └──────────────────────────────────────────────────────────┘      │
│                          │                                         │
│                          ▼   /proc/<pid>/mem scan                  │
└────────────────────────────────────────────────────────────────────┘
                           │
                           ▼  TTS-демон (этот же репо, в daemon/)
              piper-tts → ru_RU-irina-medium → pw-play / paplay
```

**Всё в одном репо**: Lua-аддон (`QuestSpeak/`), Python-демон (`daemon/`),
установщик (`install.sh`) и systemd-юнит (`systemd/`) живут рядом.

---

## Установка

### Требования

| Что | Зачем | Установка |
|---|---|---|
| Linux + WoW (Proton/Wine) | WoW-клиент | уже есть |
| `paplay` или `pw-play` | воспроизведение звука | `sudo apt install pulseaudio-utils` |
| `python3` ≥ 3.10 | демон | `sudo apt install python3 python3-venv` |
| `piper-tts` + `onnxruntime` | TTS-движок | ставится автоматически в `.venv` |
| ~100 МБ свободного места | модель + venv | — |

### Один скрипт — и готово

```bash
git clone git@github.com:Icetlin/WorldOfWarcraft.git
cd WorldOfWarcraft
./install.sh
```

`install.sh` сделает четыре вещи:

1. **Скопирует аддон** в `Interface/AddOns/QuestSpeak/` (путь определит
   автоматически — Steam-Proton, Bottles, Lutris или Wine; либо спросит
   вручную).
2. **Создаст `.venv/`** и поставит зависимости: `psutil`, `piper-tts`,
   `onnxruntime`.
3. **Скачает piper-модель** `ru_RU-irina-medium` (~15 МБ) в `models/`,
   если её ещё нет.
4. **Прогонит `check`** — проверит, что всё импортируется и модель
   загружается. Спросит, ставить ли systemd user-unit.

Переменные окружения, которые `install.sh` уважает:

* `QUEST_TTS_WOW_ADDONS` — путь к `Interface/AddOns`, если авто-детект
  не сработал.

### Где лежит каталог AddOns

Зависит от того, как вы запускаете WoW:

| Лаунчер | Путь к `Interface/AddOns` |
|---|---|
| Steam (Proton) | `~/.steam/steam/steamapps/compatdata/4201819506/pfx/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns` |
| Bottles | `~/.var/app/com.usebottles.bottles/data/bottles/bottles/<bottle>/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns` |
| Lutris | `~/Games/world-of-warcraft/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns` |
| Native Wine | `~/.wine/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns` |

`<appid>` для WoW в Steam — **1142490** (Retail) или **3565430**
(Midnight-канал, если отдельно). Под Proton «World of Warcraft» в Steam
использует нестандартный compatdata-каталог `4201819506`, и `install.sh`
уже это знает.

### Ручная установка (без `install.sh`)

```bash
# Скопируйте папку QuestSpeak/ в AddOns:
cp -r QuestSpeak "/путь/до/AddOns/"

# Или symlink, чтобы не копировать при каждом обновлении:
ln -s "$(pwd)/QuestSpeak" "/путь/до/AddOns/QuestSpeak"

# Python-окружение:
python3 -m venv .venv
source .venv/bin/activate
pip install -r daemon/requirements.txt

# Piper-модель:
mkdir -p models
curl -L -o models/ru_RU-irina-medium.onnx        https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx
curl -L -o models/ru_RU-irina-medium.onnx.json   https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx.json
```

В игре: **Esc → AddOns** → найдите `QuestSpeak — Озвучка квестов (русский)`
→ включите. Нажмите `/reload`.

---

## Использование

### Slash-команды в игре

| Команда | Что делает |
|---|---|
| `/qs on` | Включить аддон |
| `/qs off` | Выключить |
| `/qs test` | Отправить тестовую фразу в буфер |
| `/qs status` | Показать текущие флаги и текущий буфер |
| `/qs buffer` | Показать сырой буфер (для отладки) |
| `/qs log` | Последние 10 записей лога |
| `/qs debug` | Вкл/выкл подробный лог сбора текста |
| `/qs obj` / `/qs reward` / `/qs dialog` / `/qs greeting` / `/qs progress` | Тоггли отдельных типов озвучки |

Полные алиасы: `/questspeak`, `/qstts`, `/qspk` — все четыре работают.

### Команды демона

```bash
# Из корня репо (после source .venv/bin/activate):
python3 -m daemon.quest_tts_daemon                # рабочий режим (watch)
python3 -m daemon.quest_tts_daemon check          # проверить окружение
python3 -m daemon.quest_tts_daemon test "Привет"  # одна фраза в TTS
python3 -m daemon.quest_tts_daemon list           # какие голоса установлены
python3 -m daemon.quest_tts_daemon --debug watch  # с подробным логом
```

### Что озвучивается

* `QUEST_DETAIL` — заголовок и текст нового квеста
* `QUEST_PROGRESS` — обновление прогресса активного квеста
* `QUEST_COMPLETE` — квест выполнен + награды
* `GOSSIP_SHOW` — текст диалога NPC и список вариантов (если включён `readDialog`)
* `QUEST_GREETING` — приветствие с несколькими квестами (если включён `readGreeting`)

Опционально добавляются:
* `Цели: ...` — список objectives (если `readObjectives`)
* `Награды: ...` — описание наград (если `readRewards`)

Все флаги тогглятся в SavedVariables и применяются на лету.

### Запуск демона

```bash
# Разово, вручную:
./.venv/bin/python3 -m daemon.quest_tts_daemon

# Как systemd user-unit (если согласились при install.sh):
systemctl --user enable --now quest-tts.service
journalctl --user -u quest-tts -f        # смотреть логи
```

Демон **ждёт** WoW: если игра не запущена, он не падает, а спит и
периодически проверяет наличие процесса. Если WoW крашнулся — найдёт
новый PID при следующем запуске.

---

## Совместимость с Midnight (12.0.5+)

В Midnight Blizzard поменял несколько вещей, и простое копирование старого
аддона **не работает**. Вот что пришлось учесть:

1. **Strict-globals** — присваивать новые глобалы на верхнем уровне нельзя
   (`_G.SLASH_QUESTDIAG1 = "/qdiag"` молча падает). Поэтому все данные
   хранятся в полях `CreateFrame("Frame", "QuestSpeakDataFrame").text` /
   `._log` / `._db`. Поля фрейма — это не глобалы, на них ограничение не
   распространяется.

2. **Frozen SavedVariables** — в SavedVariables нельзя добавить новое поле
   (после первой записи таблица «замораживается»). Поэтому лог пишется
   в `dataFrame._log`, а не в `QuestSpeakDB._log`.

3. **`DEFAULT_CHAT_FRAME:AddMessage()`** молча игнорируется. Используется
   `print()`.

4. **`QUEST_OFFER` удалён** — регистрация падает с
   `Attempt to register unknown event "QUEST_OFFER"`. Не регаем.

5. **`GetText()` возвращает пусто** для тела квеста. Правильный API —
   `GetQuestText()`. Список проверен по исходникам аддона Immersion
   (`Immersion/Interface.lua`).

6. **`...` внутри `pcall(function() ... end)` недоступен** (вложенная
   функция — не vararg). Сигнатура `OnEvent` использует фиксированные
   параметры: `function(self, event, arg1)`, иначе первый аргумент
   события (имя аддона) теряется.

Полный путь диагностики — в git-истории коммитов.

---

## Как работает IPC с демоном

Lua кладёт текст в `dataFrame.text` в формате:

```
§QS§Никакого праздника. Точно-точно! Я видел, как...§/QS§
```

Маркеры `§QS§` и `§/QS§` — уникальная UTF-8 последовательность, которая
вряд ли встретится в обычном игровом тексте. Демон сканирует
`/proc/<wow-pid>/mem` каждые 400 мс, ищет эти маркеры в heap-регионах и
отдаёт найденный текст в TTS-очередь.

Lua ничего не знает про демона — односторонний канал. Это сделано
специально: в Midnight нельзя ни открыть файл, ни сделать сокет, ни
вызвать внешний процесс из аддона.

---

## Голоса

### Silero (по умолчанию) — натуральный русский

**Рекомендуемый бэкенд.** Российская модель, обученная преимущественно
на русском — голоса звучат как живые дикторы, не «TTS». На 5–10-сек
квестовых фразах CPU inference занимает ~0.3 сек, VRAM не используется
(остаётся WoW'у). Дефолт: `auto` → silero → piper → espeak.

Голоса (задаются через `QUEST_TTS_SILERO_SPEAKER` или в `daemon/config.py`).
Суффикс `_v2` обязателен — в silero v4 нет «голых» имён:

| Голос | Пол | Характер |
|---|---|---|
| `baya_v2` (default) | ж | молодой, мягкий, хорошо для квестовых диалогов |
| `irina_v2` | ж | тёплый, нарративный |
| `kseniya_v2` | ж | деловой, чёткая артикуляция |
| `natasha_v2` | ж | спокойный, ровный |
| `aidar_v2` | м | деловой, нейтральный |
| `ruslan_v2` | м | бас, басовитый |

Есть также варианты `*_8khz` и `*_16khz` — ниже качество, быстрее синтез
(на 5-секундной квестовой фразе разницы не слышно, а на 8 ГБ ноуте с
запущенной WoW каждый мегабайт VRAM на счету).

Переключение голоса:

```bash
# На лету, через env:
QUEST_TTS_SILERO_SPEAKER=aidar_v2 .venv/bin/python3 -m daemon.quest_tts_daemon

# Или в ~/.zshrc (для алиаса):
export QUEST_TTS_SILERO_SPEAKER=irina_v2
```

Зависимости: `torch>=2.0` (~800 МБ CPU-сборка из PyPI, ~2 ГБ с CUDA).
При первом запуске модель (~50 МБ) качается в `~/.cache/torch/hub/`.

### Piper — fallback, если нет torch

Piper работает без torch (только `onnxruntime`). Голоса скачиваются
`install.sh` в `models/`. Дефолт — `ru_RU-irina-medium` (60 МБ, базовое
качество, звучит роботизированно). Лучше irina/high (~110 МБ, та же
голосовая модель, обучена дольше):

```bash
curl -fL -o models/ru_RU-irina-high.onnx      https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/high/ru_RU-irina-high.onnx
curl -fL -o models/ru_RU-irina-high.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/high/ru_RU-irina-high.onnx.json
export QUEST_TTS_VOICE=$(pwd)/models/ru_RU-irina-high.onnx
```

Другие русские голоса piper:

```bash
huggingface-cli download rhasspy/piper-voices \
  --include 'ru/ru_RU/dmitri/medium/*' --local-dir models/
```

Принудительно использовать piper:

```bash
.venv/bin/python3 -m daemon.quest_tts_daemon --backend piper
```

### espeak-ng — последний fallback

Если ни torch, ни piper недоступны — `espeak-ng`. Качество сильно ниже
(формантный синтез, не нейросеть), зато работает без больших моделей:

```bash
sudo apt install espeak-ng
.venv/bin/python3 -m daemon.quest_tts_daemon --backend espeak
```

---

## Troubleshooting

### Аддон загружается, но в чате пусто

Скорее всего, файл `.lua` не выполнился до конца. Откройте
`<WoW>/_retail_/Logs/FrameXML.log` — там будет точная строка ошибки.
В 90% случаев это:

* `cannot use '...' outside a vararg function` → сигнатура `OnEvent` должна
  быть `function(self, event, arg1)`, а не `function(self, event, ...)`.
* `Attempt to register unknown event "X"` → удалите `X` из списка events.

### `/qs test` работает, демон молчит

Демон не находит маркер в памяти. Запустите с `--debug`:

```bash
.venv/bin/python3 -m daemon.quest_tts_daemon --debug watch
```

Если регионов памяти слишком мало — в `daemon/config.py` поставьте
`SCAN_HEAP_ONLY = False`.

### Lua-ошибка «attempt to index a nil value (global 'SLASH_X1')»

Midnight удалил/спрятал эту глобальную переменную. Не используйте
таблицу `SLASH_X` напрямую — присваивайте новые через `_G.SLASH_xxx1`.

### Озвучка обрывается

В очереди TTS лежит максимум одна фраза (`maxsize=1`): новый квест
вытесняет предыдущий. Чтобы слышать каждое событие целиком — поменяйте
`maxsize` в `daemon/tts_engine.py`.

### Звук не идёт

```bash
pactl info
pw-play /usr/share/sounds/sound-icons/glass-water-1.wav   # PipeWire
paplay /usr/share/sounds/sound-icons/glass-water-1.wav   # PulseAudio
```

Если не работает — проблема в звуковой подсистеме, не в демоне.

### `OSError: [Errno 13] Permission denied` при чтении `/proc/<pid>/mem`

Современные ядра (5.8+) требуют `ptrace_scope = 0`. Проверьте:

```bash
cat /proc/sys/kernel/yama/ptrace_scope
# 0 — можно, ≥1 — нельзя
```

Решение (`/etc/sysctl.d/99-ptrace.conf`):

```
kernel.yama.ptrace_scope = 0
```

Применить: `sudo sysctl --system`.

### `huggingface-cli` ругается «deprecated»

Работает, но рекомендуют `hf`. Если хочется — `pip install -U
huggingface_hub`, далее `hf download ...` вместо `huggingface-cli
download ...`. В `install.sh` команда `huggingface-cli` стоит первой, а
на новых версиях падает на `hf` — можно отредактировать.

---

## Структура

```
WorldOfWarcraft/
├── README.md                     # это
├── LICENSE                       # MIT
├── install.sh                    # установщик (копирует аддон, venv, модель)
├── QuestSpeak/                   # Lua-аддон
│   ├── QuestSpeak.toc
│   └── QuestSpeak.lua
├── daemon/                       # Python-демон
│   ├── __init__.py
│   ├── config.py                 # настройки
│   ├── wow_memory.py             # чтение /proc/<pid>/mem
│   ├── tts_engine.py             # piper / espeak + воспроизведение
│   ├── quest_tts_daemon.py       # главный цикл
│   └── requirements.txt
├── systemd/
│   └── quest-tts.service         # unit для автозапуска
├── models/                       # piper-модели (создаётся install.sh, .gitignore)
└── .venv/                        # Python venv (создаётся install.sh, .gitignore)
```

---

## Сравнение с OCR-подходом

| | OCR (Tesseract) | QuestSpeak |
|---|---|---|
| Точность | 80–95% (зависит от шрифта/темы) | 100% (читает из API) |
| Задержка | 0.5–3 сек (скриншот + tesseract) | ~0.4 сек (сканирование памяти) |
| CPU/GPU | tesseract жрёт ядра | piper жрёт ядра только во время синтеза |
| Ложные срабатывания | часто (любой текст на экране) | нет (только квестовый) |
| Зависимость от UI | полная | нулевая |
| Поддержка русского | так же | лучше (piper нейросетевой) |

---

## Лицензия

MIT. Голоса piper распространяются по собственным лицензиям — см.
[huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).
