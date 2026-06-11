-- QuestSpeak.lua v1.9 — финальный чистовик: COLLECT-логи только под debug=true
-- RegisterEvent обёрнут в pcall для устойчивости.
--  * safeChat() = print() + DEFAULT_CHAT_FRAME:AddMessage (через pcall).
--    В Midnight DEFAULT_CHAT_FRAME:AddMessage() молча игнорируется,
--    но print() работает в основном чате.
--  * Лог пишется в dataFrame._log (НЕ в SavedVariables — frozen table).
--  * Глобальный pcall вокруг OnEvent.
--  * 4 уникальных slash-имени: /qs /questspeak /qstts /qspk.

-- В Midnight WoW DEFAULT_CHAT_FRAME:AddMessage() тихо ничего не выводит
-- (legacy-API остался, но молча игнорируется). print() работает.
-- Поэтому сначала пробуем print, потом AddMessage как fallback.
-- (Раньше тут была опечатка safeChat(line) → рекурсия, исправлено в v1.4.1.)
local function safeChat(line)
    pcall(print, line)
    if DEFAULT_CHAT_FRAME and DEFAULT_CHAT_FRAME.AddMessage then
        pcall(function() DEFAULT_CHAT_FRAME:AddMessage(line) end)
    end
end

safeChat("|cff88ff88[QuestSpeak]|r v1.9 loading...")

local ADDON_NAME = ...

local MARKER_OPEN  = "\194\167QS\194\167"
local MARKER_CLOSE = "\194\167/QS\194\167"

-- Persistent-фрейм для IPC с демоном. Строка с маркерами живёт в .text.
local dataFrame = CreateFrame("Frame", "QuestSpeakDataFrame")
dataFrame.text = ""
-- Лог пишем ТУДА ЖЕ — поле фрейма, не SavedVariables (frozen в Midnight).
dataFrame._log = {}

----------------------------------------------------------------------
-- Logger. Все присваивания — в поля фрейма, не в глобалы и не
-- в SavedVariables. Каждая операция обёрнута в pcall, чтобы одна
-- упавшая строка не валила всё.
----------------------------------------------------------------------

local function log(msg)
    local ok, err = pcall(function()
        local stamp = "?";
        pcall(function() stamp = date("%H:%M:%S") end)
        local entry = "[" .. stamp .. "] " .. tostring(msg)
        dataFrame._log = dataFrame._log or {}
        table.insert(dataFrame._log, 1, entry)
        while #dataFrame._log > 30 do
            table.remove(dataFrame._log)
        end
        safeChat("|cff88ff88[QuestSpeak]|r " .. entry)
    end)
    if not ok then
        -- Совсем плохо: даже log() упала. Хоть что-то скажем.
        pcall(print, "[QuestSpeak LOG ERROR] " .. tostring(err))
    end
end

-- Вспомогательная: показать лог в чат
local function dumpLog()
    if not dataFrame._log or #dataFrame._log == 0 then
        safeChat("|cff88ff88[QuestSpeak]|r (лог пуст)")
        return
    end
    safeChat("|cff88ff88[QuestSpeak]|r === последние " ..
        math.min(10, #dataFrame._log) .. " записей ===")
    for i = 1, math.min(10, #dataFrame._log) do
        safeChat("  " .. dataFrame._log[i])
    end
end

----------------------------------------------------------------------
-- Прочее состояние
----------------------------------------------------------------------

local lastSpoken = {}
local DEDUP_WINDOW = 4.0

local function cleanText(text)
    if not text or text == "" then return "" end
    text = tostring(text)
    text = text:gsub("|c%x%x%x%x%x%x%x%x", "")
    text = text:gsub("|C%x%x%x%x%x%x%x%x", "")
    text = text:gsub("|r", "")
    text = text:gsub("|H.-|h%[(.-)%]|h", "%1")
    text = text:gsub("|H.-|h(.+)|h", "%1")
    text = text:gsub("|h", "")
    text = text:gsub("|T.-|t", "")
    text = text:gsub("|n", ". ")
    text = text:gsub("\n", " ")
    text = text:gsub("%s+", " ")
    text = text:match("^%s*(.-)%s*$") or ""
    return text
end

local function isEmpty(s) return s == nil or s == "" end
local function safeCall(fn, ...)
    if type(fn) == "function" then return fn(...) end
    return nil
end

local function pushToBuffer(text, eventName, questKey)
    if not QuestSpeakDB or not QuestSpeakDB.enabled then return end
    if isEmpty(text) then return end
    local now = GetTime()
    local hash = (questKey or "?") .. "|" .. text
    local prev = lastSpoken[questKey]
    if prev and prev.hash == hash and (now - prev.time) < DEDUP_WINDOW then
        return
    end
    lastSpoken[questKey] = { hash = hash, time = now }
    dataFrame.text = MARKER_OPEN .. text .. MARKER_CLOSE
    log(eventName .. " (" .. tostring(questKey) .. "): " ..
        text:sub(1, 80) .. (text:len() > 80 and "..." or ""))
end

----------------------------------------------------------------------
-- Сборщики текста (без изменений)
----------------------------------------------------------------------

-- Чтение квестового текста. Список API взят из Immersion/Interface.lua
-- (он единственный, кто реально читает квестовый UI в Midnight).
--
-- Ключевая находка: в WoW тело квеста читается через GetQuestText(),
-- а НЕ через GetText() (это общий метод виджета, в Midnight пуст).
-- Также НЕ гейтимся на IsQuestDetailVisible() — если стоит Immersion,
-- Blizzard-фрейм скрыт и функция возвращает false, хотя квест активен.
local function collectQuestDetail()
    local parts = {}
    local title = cleanText(safeCall(GetTitleText))
    if not isEmpty(title) and not title:find("^[%d]+%.%s") then
        table.insert(parts, title)
    end
    local body = cleanText(safeCall(GetQuestText))  -- ← FIX: было GetText()
    if not isEmpty(body) then table.insert(parts, body) end
    if QuestSpeakDB.readObjectives then
        local obj = cleanText(safeCall(GetObjectiveText))
        if not isEmpty(obj) then table.insert(parts, "Цели: " .. obj) end
    end
    if QuestSpeakDB.readRewards then
        local reward = cleanText(safeCall(GetRewardText))
        if not isEmpty(reward) then table.insert(parts, "Награды: " .. reward) end
    end
    return table.concat(parts, ". "), ("detail:" .. (title or "?"))
end

local function collectQuestProgress()
    local parts = {}
    local title = cleanText(safeCall(GetActiveTitleText))
    if not isEmpty(title) then table.insert(parts, title) end
    local body = cleanText(safeCall(GetActiveQuestText))
    if not isEmpty(body) then table.insert(parts, body) end
    if QuestSpeakDB.readProgress and QuestSpeakDB.readObjectives then
        local obj = cleanText(safeCall(GetObjectiveText))
        if not isEmpty(obj) then table.insert(parts, "Прогресс: " .. obj) end
    end
    return table.concat(parts, ". "), ("progress:" .. (title or "?"))
end

local function collectQuestComplete()
    local parts = {}
    local title = cleanText(safeCall(GetTitleText))
    if not isEmpty(title) then
        table.insert(parts, "Квест выполнен: " .. title)
    end
    local body = cleanText(safeCall(GetQuestText))  -- ← FIX: было GetText()
    if not isEmpty(body) then table.insert(parts, body) end
    if QuestSpeakDB.readRewards then
        local reward = cleanText(safeCall(GetRewardText))
        if not isEmpty(reward) then table.insert(parts, "Награды: " .. reward) end
    end
    return table.concat(parts, ". "), ("complete:" .. (title or "?"))
end

local function collectGossip()
    local parts = {}
    local text = cleanText(safeCall(GetGossipText))
    if not isEmpty(text) then table.insert(parts, text) end
    if QuestSpeakDB.readDialog then
        local n = safeCall(GetNumGossipOptions) or 0
        for i = 1, n do
            local info = safeCall(GetGossipInfo, i)
            local name = type(info) == "table" and info.name or nil
            name = cleanText(name)
            if not isEmpty(name) then
                table.insert(parts, "Вариант " .. i .. ": " .. name)
            end
        end
    end
    return table.concat(parts, ". "), "gossip"
end

local function collectQuestGreeting()
    if not (IsQuestGreetingVisible and IsQuestGreetingVisible()) then return nil end
    local parts = {}
    local nActive = safeCall(GetNumActiveQuests) or 0
    for i = 1, nActive do
        local title = cleanText(safeCall(GetActiveTitleText, i))
        local body  = cleanText(safeCall(GetActiveQuestText, i))
        if not isEmpty(title) then
            table.insert(parts, "Активный квест: " .. title)
        end
        if not isEmpty(body) then table.insert(parts, body) end
    end
    local nAvail = safeCall(GetNumAvailableQuests) or 0
    for i = 1, nAvail do
        local title = cleanText(safeCall(GetAvailableTitleText, i))
        local body  = cleanText(safeCall(GetAvailableQuestText, i))
        if not isEmpty(title) then
            table.insert(parts, "Доступен квест: " .. title)
        end
        if not isEmpty(body) then table.insert(parts, body) end
    end
    return table.concat(parts, ". "), "greeting"
end

----------------------------------------------------------------------
-- Frame
----------------------------------------------------------------------

local frame = CreateFrame("Frame", "QuestSpeakFrame", UIParent)

local events = {
    "ADDON_LOADED",
    "PLAYER_LOGOUT",
    "QUEST_DETAIL",
    "QUEST_PROGRESS",
    "QUEST_COMPLETE",
    "QUEST_GREETING",
    "GOSSIP_SHOW",
    -- ВАЖНО: QUEST_OFFER удалён в Midnight ("Attempt to register unknown
    -- event 'QUEST_OFFER'" в A7b v1.4.4). Принять квест теперь приходит
    -- через QUEST_DETAIL, поэтому ничего не теряем.
}
for _, e in ipairs(events) do
    local ok, err = pcall(frame.RegisterEvent, frame, e)
    if not ok then
        -- Некоторые события могут быть удалены Blizzard в новых билдах.
        -- Пишем в лог, но НЕ роняем файл — оставшиеся события продолжат работать.
        pcall(print, "|cffff8800[QuestSpeak]|r RegisterEvent '" ..
            tostring(e) .. "' failed: " .. tostring(err))
    end
end

frame:SetScript("OnEvent", function(self, event, arg1)
    -- Глобальный pcall: любая ошибка в обработчике не валит аддон,
    -- а уходит в лог.
    --
    -- Сигнатура: function(self, event, arg1) — фиксированное число
    -- параметров. ВАЖНО: если бы оставили function(self, event, ...),
    -- то '...' внутри pcall(function() ... end) был бы недоступен
    -- (вложенная функция — не vararg). Это ломало бы ADDON_LOADED,
    -- потому что первый аргумент события (имя аддона) приходил бы
    -- через '...'. См. FrameXML.log от 00:16 — там был именно этот баг.
    pcall(function()
        if event == "ADDON_LOADED" then
            local loaded = arg1
            if loaded == ADDON_NAME then
                QuestSpeakDB = QuestSpeakDB or {}
                QuestSpeakDB.enabled        = (QuestSpeakDB.enabled        ~= false)
                QuestSpeakDB.readObjectives = (QuestSpeakDB.readObjectives ~= false)
                QuestSpeakDB.readRewards    = (QuestSpeakDB.readRewards    ~= false)
                QuestSpeakDB.readDialog     = (QuestSpeakDB.readDialog     ~= false)
                QuestSpeakDB.readGreeting   = (QuestSpeakDB.readGreeting   ~= false)
                QuestSpeakDB.readProgress   = (QuestSpeakDB.readProgress   ~= false)
                QuestSpeakDB.debug          = (QuestSpeakDB.debug          == true)
                dataFrame.text = ""

                -- Логгируем факт загрузки + диагностику slash-регистрации
                safeChat("|cff88ff88[QuestSpeak]|r v1.9 loaded")
                log("ADDON_LOADED " .. tostring(ADDON_NAME))
                log("SLASH_QUESTSPEAKTTS1=" .. tostring(SLASH_QUESTSPEAKTTS1))
                log("SLASH_QUESTSPEAKTTS2=" .. tostring(SLASH_QUESTSPEAKTTS2))
                log("SLASH_QUESTSPEAKTTS3=" .. tostring(SLASH_QUESTSPEAKTTS3))
                log("SLASH_QUESTSPEAKTTS4=" .. tostring(SLASH_QUESTSPEAKTTS4))
                log("handler=" .. tostring(SlashCmdList and SlashCmdList.QUESTSPEAKTTS))
            end
            return
        end

        if event == "PLAYER_LOGOUT" then
            dataFrame.text = ""
            return
        end

        if not QuestSpeakDB then
            return  -- OnEvent сработал ДО ADDON_LOADED
        end
        if not QuestSpeakDB.enabled then return end

        if event == "QUEST_DETAIL" then
            log("event QUEST_DETAIL arg1=" .. tostring(arg1))
            C_Timer.After(0.5, function()
                local text, key = collectQuestDetail(arg1)
                if QuestSpeakDB.debug then
                    log("COLLECT detail: text_len=" .. tostring(text and #text or "nil") ..
                        " key=" .. tostring(key) ..
                        " preview=" .. tostring(text and text:sub(1, 40) or "nil"))
                end
                pushToBuffer(text, "QUEST_DETAIL", key)
            end)
        elseif event == "QUEST_PROGRESS" then
            log("event QUEST_PROGRESS")
            C_Timer.After(0.1, function()
                local text, key = collectQuestProgress()
                if QuestSpeakDB.debug then
                    log("COLLECT progress: text_len=" .. tostring(text and #text or "nil"))
                end
                pushToBuffer(text, "QUEST_PROGRESS", key)
            end)
        elseif event == "QUEST_COMPLETE" then
            log("event QUEST_COMPLETE")
            C_Timer.After(0.1, function()
                local text, key = collectQuestComplete()
                if QuestSpeakDB.debug then
                    log("COLLECT complete: text_len=" .. tostring(text and #text or "nil"))
                end
                pushToBuffer(text, "QUEST_COMPLETE", key)
            end)
        elseif event == "GOSSIP_SHOW" then
            log("event GOSSIP_SHOW")
            C_Timer.After(0.1, function()
                if not QuestSpeakDB.readDialog then return end
                local text, key = collectGossip()
                if QuestSpeakDB.debug then
                    log("COLLECT gossip: text_len=" .. tostring(text and #text or "nil"))
                end
                pushToBuffer(text, "GOSSIP_SHOW", key)
            end)
        elseif event == "QUEST_GREETING" then
            log("event QUEST_GREETING")
            C_Timer.After(0.1, function()
                if not QuestSpeakDB.readGreeting then return end
                local text, key = collectQuestGreeting()
                if QuestSpeakDB.debug then
                    log("COLLECT greeting: text_len=" .. tostring(text and #text or "nil"))
                end
                pushToBuffer(text, "QUEST_GREETING", key)
            end)
        end
    end)
end)

----------------------------------------------------------------------
-- Slash-команды: 4 уникальных имени
----------------------------------------------------------------------

SLASH_QUESTSPEAKTTS1 = "/qs"
SLASH_QUESTSPEAKTTS2 = "/questspeak"
SLASH_QUESTSPEAKTTS3 = "/qstts"
SLASH_QUESTSPEAKTTS4 = "/qspk"

SlashCmdList.QUESTSPEAKTTS = function(rawMsg)
    -- pcall — на случай, если обработчик упал. Тогда ошибка идёт в лог.
    local ok, err = pcall(function()
        local msg = (rawMsg or ""):lower():match("^%s*(.-)%s*$") or ""
        log("slash: '" .. tostring(msg) .. "'")

        if msg == "on" then
            if QuestSpeakDB then QuestSpeakDB.enabled = true end
            safeChat("|cff88ff88[QuestSpeak]|r Включён")
        elseif msg == "off" then
            if QuestSpeakDB then QuestSpeakDB.enabled = false end
            safeChat("|cff88ff88[QuestSpeak]|r Выключён")
        elseif msg == "test" then
            pushToBuffer("Это тестовая фраза для проверки озвучки QuestSpeak.",
                         "TEST", "test")
        elseif msg == "debug" then
            if QuestSpeakDB then
                QuestSpeakDB.debug = not QuestSpeakDB.debug
                safeChat("|cff88ff88[QuestSpeak]|r Debug: " ..
                    tostring(QuestSpeakDB.debug))
            end
        elseif msg == "log" then
            dumpLog()
        elseif msg == "check" then
            safeChat("|cff88ff88[QuestSpeak]|r === check ===")
            safeChat("  SLASH1=" .. tostring(SLASH_QUESTSPEAKTTS1))
            safeChat("  SLASH2=" .. tostring(SLASH_QUESTSPEAKTTS2))
            safeChat("  SLASH3=" .. tostring(SLASH_QUESTSPEAKTTS3))
            safeChat("  SLASH4=" .. tostring(SLASH_QUESTSPEAKTTS4))
            safeChat("  handler=" .. tostring(SlashCmdList.QUESTSPEAKTTS))
            safeChat("  dataFrame.text='" .. tostring(dataFrame.text):sub(1,80) .. "'")
        elseif msg == "status" then
            safeChat("|cff88ff88[QuestSpeak]|r === Статус ===")
            if QuestSpeakDB then
                safeChat("  enabled       : " .. tostring(QuestSpeakDB.enabled))
                safeChat("  readObjectives: " .. tostring(QuestSpeakDB.readObjectives))
                safeChat("  readRewards   : " .. tostring(QuestSpeakDB.readRewards))
                safeChat("  readDialog    : " .. tostring(QuestSpeakDB.readDialog))
                safeChat("  readGreeting  : " .. tostring(QuestSpeakDB.readGreeting))
                safeChat("  readProgress  : " .. tostring(QuestSpeakDB.readProgress))
            end
            safeChat("  buffer: " .. tostring(dataFrame.text):sub(1, 120))
        elseif msg == "obj" or msg == "objectives" then
            if QuestSpeakDB then
                QuestSpeakDB.readObjectives = not QuestSpeakDB.readObjectives
                safeChat("|cff88ff88[QuestSpeak]|r Цели: " ..
                    tostring(QuestSpeakDB.readObjectives))
            end
        elseif msg == "reward" or msg == "rewards" then
            if QuestSpeakDB then
                QuestSpeakDB.readRewards = not QuestSpeakDB.readRewards
                safeChat("|cff88ff88[QuestSpeak]|r Награды: " ..
                    tostring(QuestSpeakDB.readRewards))
            end
        elseif msg == "dialog" or msg == "gossip" then
            if QuestSpeakDB then
                QuestSpeakDB.readDialog = not QuestSpeakDB.readDialog
                safeChat("|cff88ff88[QuestSpeak]|r Диалоги: " ..
                    tostring(QuestSpeakDB.readDialog))
            end
        elseif msg == "greeting" then
            if QuestSpeakDB then
                QuestSpeakDB.readGreeting = not QuestSpeakDB.readGreeting
                safeChat("|cff88ff88[QuestSpeak]|r Quest Greeting: " ..
                    tostring(QuestSpeakDB.readGreeting))
            end
        elseif msg == "progress" then
            if QuestSpeakDB then
                QuestSpeakDB.readProgress = not QuestSpeakDB.readProgress
                safeChat("|cff88ff88[QuestSpeak]|r Quest Progress: " ..
                    tostring(QuestSpeakDB.readProgress))
            end
        elseif msg == "buffer" then
            safeChat("|cff88ff88[QuestSpeak]|r " ..
                tostring(dataFrame.text))
        else
            safeChat("|cff88ff88[QuestSpeak]|r Команды:")
            safeChat("  on|off, test, status, log, check, buffer")
            safeChat("  obj|reward|dialog|greeting|progress")
            safeChat("Алиасы: /qs, /questspeak, /qstts, /qspk")
        end
    end)
    if not ok then
        safeChat(
            "|cffff4444[QuestSpeak ERROR]|r " .. tostring(err))
        log("SLASH ERROR: " .. tostring(err))
    end
end

safeChat("|cff88ff88[QuestSpeak]|r v1.9 ready, " ..
    "slashes: /qs /questspeak /qstts /qspk")
