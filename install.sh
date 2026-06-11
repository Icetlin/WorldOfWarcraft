#!/usr/bin/env bash
# quest_tts installer
# ─────────────────────
#  1. Копирует Lua-аддон в Wine/Proton-папку WoW
#  2. Создаёт Python venv и ставит зависимости
#  3. Скачивает русскую piper-модель (если её ещё нет)
#  4. Опционально: создаёт systemd user-unit для автозапуска демона
#
# Запускать от обычного пользователя (НЕ от root).

set -euo pipefail

# ─── Цвета и лог ──────────────────────────────────────────────────
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
CYA='\033[0;36m'
RST='\033[0m'

log()  { echo -e "${CYA}[quest_tts]${RST} $*"; }
ok()   { echo -e "${GRN}[ok]${RST} $*"; }
warn() { echo -e "${YLW}[warn]${RST} $*"; }
err()  { echo -e "${RED}[err]${RST} $*" >&2; }

# ─── Преамбула ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
ADDON_SRC="$PROJECT_ROOT/wow_addon/QuestSpeak"
VENV_DIR="$PROJECT_ROOT/.venv"
MODELS_DIR="$PROJECT_ROOT/models"
SYSTEMD_DIR="$HOME/.config/systemd/user"

log "Проект: $PROJECT_ROOT"

# ─── 1. Куда ставить аддон ────────────────────────────────────────
detect_wow_addons_dir() {
    # 1) Переменная окружения
    if [ -n "${QUEST_TTS_WOW_ADDONS:-}" ] && [ -d "${QUEST_TTS_WOW_ADDONS}" ]; then
        echo "${QUEST_TTS_WOW_ADDONS}"
        return 0
    fi

    # 2) Стандартный Steam-Proton путь: 4201819506 = "World of Warcraft"
    local candidates=(
        "$HOME/.steam/steam/steamapps/compatdata/4201819506/pfx/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns"
        "$HOME/.local/share/Steam/steamapps/compatdata/4201819506/pfx/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns"
        # 3) Battle.net через Bottles
        "$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles/BattleNet/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns"
        # 4) Lutris
        "$HOME/Games/world-of-warcraft/drive_c/Program Files (x86)/World of Warcraft/_retail_/Interface/AddOns"
    )

    for d in "${candidates[@]}"; do
        if [ -d "$d" ]; then
            echo "$d"
            return 0
        fi
    done

    return 1
}

if WOW_ADDONS=$(detect_wow_addons_dir); then
    ok "Найден WoW AddOns каталог: $WOW_ADDONS"
    mkdir -p "$WOW_ADDONS"
    rm -rf "$WOW_ADDONS/QuestSpeak"
    cp -r "$ADDON_SRC" "$WOW_ADDONS/QuestSpeak"
    ok "Аддон QuestSpeak скопирован в $WOW_ADDONS/QuestSpeak"
else
    warn "WoW-клиент не найден автоматически. Это нормально, если вы"
    warn "ещё не установили игру. Скопируйте папку вручную:"
    warn "  $ADDON_SRC"
    warn "→  <путь-к-WoW>/_retail_/Interface/AddOns/QuestSpeak/"
    echo ""
    read -r -p "Указать путь к AddOns вручную? (y/N): " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        read -r -p "Путь к AddOns: " WOW_ADDONS
        if [ -d "$WOW_ADDONS" ]; then
            cp -r "$ADDON_SRC" "$WOW_ADDONS/QuestSpeak"
            ok "Скопировано в $WOW_ADDONS/QuestSpeak"
        else
            err "Каталог не существует: $WOW_ADDONS"
            exit 1
        fi
    fi
fi

# ─── 2. Python venv + зависимости ─────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    log "Создаю venv: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

log "Ставлю Python-зависимости…"
pip install --quiet --upgrade pip
pip install --quiet -r "$PROJECT_ROOT/daemon/requirements.txt"
ok "Зависимости установлены"

# Проверим, что piper-tts импортируется
if ! python3 -c "import piper" 2>/dev/null; then
    err "piper-tts не импортируется. Попробуйте вручную:"
    err "  source $VENV_DIR/bin/activate && pip install piper-tts"
    exit 1
fi
ok "piper-tts доступен"

# ─── 3. Piper-модель для русского ─────────────────────────────────
mkdir -p "$MODELS_DIR"

VOICE_BASE="ru_RU-irina-medium"
VOICE_ONNX="$MODELS_DIR/${VOICE_BASE}.onnx"
VOICE_JSON="$MODELS_DIR/${VOICE_BASE}.onnx.json"

if [ -f "$VOICE_ONNX" ] && [ -f "$VOICE_JSON" ]; then
    ok "Piper-модель уже на месте: $VOICE_ONNX"
else
    log "Скачиваю piper-модель $VOICE_BASE (~60 МБ) через curl…"
    BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU/irina/medium"

    # Почему curl, а не huggingface-cli:
    # утилита `huggingface-cli` в huggingface_hub ≥ 1.0 помечена
    # «deprecated — no longer works» и вместо скачивания печатает help.
    # Новая `hf` требует свежей версии huggingface_hub и не всегда
    # установлена вместе с piper-tts. curl — детерминированно, без
    # зависимостей.
    if ! command -v curl >/dev/null 2>&1; then
        err "curl не найден, поставьте его: sudo apt install curl"
        exit 1
    fi

    curl -fL --retry 3 --retry-delay 2 -o "$VOICE_ONNX" \
        "$BASE_URL/${VOICE_BASE}.onnx"
    curl -fL --retry 3 --retry-delay 2 -o "$VOICE_JSON" \
        "$BASE_URL/${VOICE_BASE}.onnx.json"

    if [ ! -s "$VOICE_ONNX" ] || [ ! -s "$VOICE_JSON" ]; then
        err "Файлы модели не скачались (пустые или недоступны). Проверьте сеть."
        exit 1
    fi
    ok "Piper-модель скачана в $MODELS_DIR ($(du -h "$VOICE_ONNX" | cut -f1))"
fi

# ─── 4. Тестовый прогон ───────────────────────────────────────────
log "Тестирую окружение…"
if python3 -m daemon.quest_tts_daemon check; then
    ok "Окружение в порядке"
else
    warn "Есть проблемы (см. выше). Демон может работать не полностью."
fi

# ─── 5. systemd user-unit (опционально) ───────────────────────────
if [ -n "${XDG_RUNTIME_DIR:-}" ] || pgrep -u "$USER" systemd-user >/dev/null 2>&1; then
    read -r -p "Создать systemd user-unit для автозапуска демона? (y/N): " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        mkdir -p "$SYSTEMD_DIR"
        cat > "$SYSTEMD_DIR/quest-tts.service" <<EOF
[Unit]
Description=QuestSpeak TTS daemon (WoW)
After=default.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
ExecStart=$VENV_DIR/bin/python3 -m daemon.quest_tts_daemon watch
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        ok "Unit создан: $SYSTEMD_DIR/quest-tts.service"
        echo ""
        echo "  Включить автозапуск:"
        echo "    systemctl --user enable --now quest-tts.service"
        echo "  Посмотреть логи:"
        echo "    journalctl --user -u quest-tts -f"
    fi
else
    warn "systemd user-инстанс не найден — пропускаю создание unit."
    warn "Запускайте демон вручную: $VENV_DIR/bin/python3 -m daemon.quest_tts_daemon"
fi

# ─── Финал ────────────────────────────────────────────────────────
cat <<EOF

${GRN}═══════════════════════════════════════════════════════════${RST}
${GRN} Установка завершена!${RST}
${GRN}═══════════════════════════════════════════════════════════${RST}

 Что дальше:

  1. Перезагрузите UI в WoW:  /reload  (или полностью перезапустите игру)
  2. В игре включите аддон QuestSpeak:  /qs on
  3. Проверьте:  /qs test
  4. Запустите демон:
       $VENV_DIR/bin/python3 -m daemon.quest_tts_daemon

 Команды демона:
   watch    рабочий режим (по умолчанию)
   check    проверить окружение
   test     прогнать тестовую фразу
   list     показать голоса

EOF
