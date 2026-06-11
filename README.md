# QuestSpeak — Озвучка квестов World of Warcraft (русский)

Lua-аддон для **World of Warcraft: Midnight (12.0.5+)**, который читает текст
квестов и диалогов NPC из игрового API и кладёт его в буфер с уникальным
маркером. Внешний демон (Python + `piper-tts`) подхватывает этот буфер из
памяти процесса и озвучивает нейросетевым голосом.

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
                           ▼  TTS-демон (отдельный проект)
              piper-tts → ru_RU-irina-medium → pw-play
```

**Репозиторий содержит только Lua-часть.** Демон (Python + piper) живёт
отдельно: <https://github.com/Icetlin/quest_tts>.

---

## Установка

### Где лежит каталог AddOns

Зависит от того, как вы запускаете WoW:

| Лаунчер | Путь к `Interface/AddOns` |
|---|---|
| Steam (Proton) | `~/.steam/steam/steamapps/compatdata/<appid>/pfx/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns` |
| Bottles | `~/.var/app/com.usebottles.bottles/data/bottles/bottles/<bottle>/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns` |
| Lutris | `~/.local/share/lutris/runners/wine/`... |
| Native Wine | `~/.wine/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns` |

`<appid>` для WoW в Steam — **1142490** (Retail) или **3565430** (Midnight-канал, если отдельно).

### Копирование

```bash
# Скопируйте всю папку QuestSpeak/ в AddOns:
cp -r QuestSpeak "/путь/до/AddOns/"

# Или symbolic link, чтобы не копировать при каждом обновлении:
ln -s "$(pwd)/QuestSpeak" "/путь/до/AddOns/QuestSpeak"
```

В игре: **Esc → AddOns** → найдите `QuestSpeak — Озвучка квестов (русский)` →
включите. Нажмите `/reload`.

---

## Использование

### Slash-команды

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
python3 -m daemon.quest_tts_daemon --debug watch
```

Если регионов памяти слишком мало, в `daemon/config.py` поставьте
`SCAN_HEAP_ONLY = False`.

### Lua-ошибка «attempt to index a nil value (global 'SLASH_X1')»

Midnight удалил/спрятал эту глобальную переменную. Не используйте
таблицу `SLASH_X` напрямую — присваивайте новые через `_G.SLASH_xxx1`.

### Озвучка обрывается

В очереди TTS лежит максимум одна фраза (`maxsize=1`): новый квест
вытесняет предыдущий. Чтобы слышать каждое событие целиком — поменяйте
`maxsize` в `tts_engine.py`.

---

## Структура

```
WorldOfWarcraft/
├── README.md                  # это
├── LICENSE                    # MIT
└── QuestSpeak/
    ├── QuestSpeak.toc         # манифест (Interface 120005, 11508, 110000)
    └── QuestSpeak.lua         # код
```

Кода здесь ровно один файл и 300 строк, потому что вся остальная работа
делается в демоне. Внутри `QuestSpeak.lua` четыре секции:

1. **Буфер и логгер** — frame, `dataFrame.text`, `dataFrame._log`
2. **Сборщики текста** — `collectQuestDetail/Progress/Complete/Greeting/Gossip`
3. **OnEvent** — глобальный pcall + диспетчер по `event`
4. **Slash-команды** — `SLASH_QUESTSPEAKTTS1..4` + `SlashCmdList.QUESTSPEAKTTS`

---

## Лицензия

MIT. Подробнее — `LICENSE`.
