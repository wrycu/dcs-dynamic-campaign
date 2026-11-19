--[[
Campaign bridge: Hooks (GameGUI) environment side.

Install on the dedicated server only, as
  Saved Games\DCS.<variant>\Scripts\Hooks\campaign_hooks.lua

What it does:
  * Reports players by stable UCID (connect, disconnect, slot changes). The mission
    environment only sees player names; the engine joins the two by name.
  * Gates slots in onPlayerTryChangeSlot from a rule table the engine pushes with the
    `slot_rules` command. Default (PvE): spectators and blue only.
  * Loads another mission when the engine asks (`load_mission`), and reports each stage of a
    mission load (`mission_load`).
  * Chat commands: chat starting with "!" goes to the engine (`chat_command`) instead of the
    other players; the engine answers with `chat_to`.
  * Puts players into aircraft (`spawn_for`, `force_slot`) and reports the server's frame times
    (`frame_stats`), for proof P19.

Optional: define a global CampaignHooksConfig table before this file loads to override `defaults`.

Protocol: see docs/protocol.md. Lua 5.1 (DCS).
]]

local function log_info(msg) log.write("CAMPAIGN", log.INFO, msg) end
local function log_error(msg) log.write("CAMPAIGN", log.ERROR, msg) end

local loaded, load_err = pcall(function()

-- The hooks API table is `Sim` since DCS 2.9.x; older builds call it `DCS`.
local Sim = Sim or DCS

local defaults = {
  host = "127.0.0.1",
  port = 7341,
  poll_interval = 0.1,       -- seconds of real time between network pumps
  reconnect_interval = 5.0,
  connect_timeout = 5.0,
  max_queue = 5000,
  retain = 2000,
  max_lines = 200,
  max_bytes = 256 * 1024,
  allowed_sides = { 0, 2 },  -- spectators, blue (PvE: red is AI-only)
  side_denied_reason = "That side is AI-only in this campaign.",
  chat_prefix = "!",         -- chat starting with this is a command for the engine
  frame_stats_interval = 10, -- seconds of real time between frame_stats events (0: off)
}

local cfg = {}
for k, v in pairs(defaults) do cfg[k] = v end
for k, v in pairs(CampaignHooksConfig or {}) do cfg[k] = v end

package.path = package.path .. ";.\\LuaSocket\\?.lua"
package.cpath = package.cpath .. ";.\\LuaSocket\\?.dll"
local socket = require("socket")

-- ======================================================================================
-- Transport. The block between the BEGIN/END markers must be identical in
-- lua/mission/campaign_bridge.lua and lua/hooks/campaign_hooks.lua (a test checks this).
-- ======================================================================================

-- BEGIN TRANSPORT

local function new_deque() return { first = 1, last = 0, items = {} } end
local function dq_size(q) return q.last - q.first + 1 end
local function dq_push(q, v) q.last = q.last + 1; q.items[q.last] = v end
local function dq_push_front(q, v) q.first = q.first - 1; q.items[q.first] = v end
local function dq_pop(q)
  if q.first > q.last then return nil end
  local v = q.items[q.first]
  q.items[q.first] = nil
  q.first = q.first + 1
  return v
end

-- opts: host, port, src, socket, encode, decode, now, log, hello_data(), on_message(msg),
--       max_queue, retain, reconnect_interval, connect_timeout, max_lines, max_bytes
local function new_transport(opts)
  local T = {
    state = "disconnected",  -- disconnected | connecting | handshake | ready
    next_attempt = 0, connect_deadline = 0,
    seq = 0, pending = new_deque(), sent = new_deque(),
    sock = nil, rx = "", tx = nil, tx_pos = 1, tx_msg = nil,
  }

  local function line_for(mtype, data, seq, t, extra)
    local env = { v = 1, type = mtype, src = opts.src, seq = seq, t = t, data = data or {} }
    if extra then for k, v in pairs(extra) do env[k] = v end end
    return opts.encode(env) .. "\n"
  end

  -- Sequenced: queued while disconnected and replayed after reconnect if the server missed it.
  function T.send(mtype, data, t)
    T.seq = T.seq + 1
    dq_push(T.pending, { seq = T.seq, line = line_for(mtype, data, T.seq, t) })
    if dq_size(T.pending) > opts.max_queue then
      dq_pop(T.pending)
      opts.log("send queue full; dropped oldest message")
    end
  end

  -- Unsequenced: only sent while connected, never replayed (telemetry, acks).
  function T.send_volatile(mtype, data, t, extra)
    if T.state ~= "ready" then return end
    dq_push(T.pending, { seq = 0, line = line_for(mtype, data, 0, t, extra) })
  end

  function T.ack(id, ok, result, err)
    T.send_volatile("ack", {}, nil, { re = id, ok = ok, result = result, error = err })
  end

  local function disconnect(reason)
    if T.sock then pcall(function() T.sock:close() end) end
    T.sock = nil
    if T.tx_msg and T.tx_msg.seq > 0 then dq_push_front(T.pending, T.tx_msg) end
    T.tx, T.tx_msg, T.tx_pos, T.rx = nil, nil, 1, ""
    if T.state == "handshake" or T.state == "ready" then
      opts.log("disconnected: " .. tostring(reason))
    end
    T.state = "disconnected"
    T.next_attempt = opts.now() + opts.reconnect_interval
  end

  local function on_connected()
    T.state = "handshake"
    T.rx = ""
    T.tx = line_for("hello", opts.hello_data(), 0, nil)
    T.tx_pos, T.tx_msg = 1, nil
    opts.log("connected to " .. opts.host .. ":" .. opts.port)
  end

  local function on_welcome(last_seq)
    -- Replay everything the server has not seen, oldest first; drop volatile messages.
    local q = new_deque()
    for _, src in ipairs({ T.sent, T.pending }) do
      for i = src.first, src.last do
        local m = src.items[i]
        if m.seq > last_seq then dq_push(q, m) end
      end
    end
    T.sent, T.pending = new_deque(), q
    T.state = "ready"
    if dq_size(q) > 0 then opts.log("replaying " .. dq_size(q) .. " messages after seq " .. last_seq) end
  end

  local function start_connect(now)
    local s = opts.socket.tcp()
    if not s then T.next_attempt = now + opts.reconnect_interval; return end
    s:settimeout(0)
    pcall(function() s:setoption("tcp-nodelay", true) end)
    T.sock = s
    local ok, err = s:connect(opts.host, opts.port)
    if ok or err == "already connected" then
      on_connected()
    elseif err == "timeout" or err == "Operation already in progress" then
      T.state = "connecting"
      T.connect_deadline = now + opts.connect_timeout
    else
      disconnect(err)
    end
  end

  local function check_connecting(now)
    local _, writable = opts.socket.select(nil, { T.sock }, 0)
    if writable and writable[1] then
      if T.sock:getpeername() then on_connected() else disconnect("connect failed") end
    elseif now > T.connect_deadline then
      disconnect("connect timeout")
    end
  end

  local function pump_recv()
    for _ = 1, opts.max_lines do
      local line, err, partial = T.sock:receive("*l", T.rx)
      if line then
        T.rx = ""
        local ok, msg = pcall(opts.decode, line)
        if not ok or type(msg) ~= "table" then
          opts.log("undecodable message dropped")
        elseif msg.type == "welcome" then
          if T.state == "handshake" then
            on_welcome(tonumber(type(msg.data) == "table" and msg.data.last_seq) or 0)
          end
        elseif T.state == "ready" then
          local hok, herr = pcall(opts.on_message, msg)
          if not hok then opts.log("message handler error: " .. tostring(herr)) end
        end
      elseif err == "timeout" then
        T.rx = partial or ""
        return true
      else
        disconnect(err)
        return false
      end
    end
    return true
  end

  local function pump_send()
    local budget = opts.max_bytes
    while budget > 0 do
      if not T.tx then
        if T.state ~= "ready" then return end
        local m = dq_pop(T.pending)
        if not m then return end
        T.tx, T.tx_pos, T.tx_msg = m.line, 1, m
      end
      local last, err, partial_last = T.sock:send(T.tx, T.tx_pos)
      if last then
        budget = budget - (last - T.tx_pos + 1)
        if T.tx_msg and T.tx_msg.seq > 0 then
          dq_push(T.sent, T.tx_msg)
          if dq_size(T.sent) > opts.retain then dq_pop(T.sent) end
        end
        T.tx, T.tx_msg, T.tx_pos = nil, nil, 1
      elseif err == "timeout" then
        if partial_last then T.tx_pos = partial_last + 1 end
        return
      else
        disconnect(err)
        return
      end
    end
  end

  function T.tick()
    local now = opts.now()
    if T.state == "disconnected" then
      if now >= T.next_attempt then start_connect(now) end
    elseif T.state == "connecting" then
      check_connecting(now)
    end
    if T.state == "handshake" or T.state == "ready" then
      if pump_recv() then pump_send() end
    end
  end

  return T
end
-- END TRANSPORT

-- ======================================================================================
-- Hooks logic
-- ======================================================================================

local function set_of(list)
  local s = {}
  for _, v in ipairs(list or {}) do s[tonumber(v)] = true end
  return s
end

local H = {
  run_id = string.format("%.0f", socket.gettime() * 1000000),  -- per DCS process start
  rules = { sides = set_of(cfg.allowed_sides), deny = {}, airdromes = {} },  -- ucid / airdrome id -> reason
  ucids = {},                                                  -- player id -> ucid
}
CampaignHooks = H

local transport = new_transport({
  host = cfg.host, port = cfg.port, src = "hooks", socket = socket,
  encode = net.lua2json, decode = net.json2lua, now = Sim.getRealTime, log = log_info,
  max_queue = cfg.max_queue, retain = cfg.retain, reconnect_interval = cfg.reconnect_interval,
  connect_timeout = cfg.connect_timeout, max_lines = cfg.max_lines, max_bytes = cfg.max_bytes,
  hello_data = function() return { proto = 1, run_id = H.run_id } end,
  on_message = function(msg) H.on_message(msg) end,
})
H.transport = transport

local function emit(kind, data)
  data.kind = kind
  transport.send("player", data, nil)
end

local commands = {}

local function reasons(t)
  local out = {}
  for k, reason in pairs(type(t) == "table" and t or {}) do out[tonumber(k) or k] = tostring(reason) end
  return out
end

-- data: { allowed_sides = { 0, 2 }, deny = { [ucid] = "reason" }, deny_airdromes = { [id] = "reason" } }
-- (replaces the whole table). deny_airdromes refuses slots DCS lists at that airdrome, which
-- includes dynamic slots if DCS asks this hook about them (proof P2 finds out).
function commands.slot_rules(d)
  H.rules = { sides = set_of(d.allowed_sides or cfg.allowed_sides), deny = reasons(d.deny),
              airdromes = reasons(d.deny_airdromes) }
end

-- Load another mission on this server. data: { file = "name.miz" (in Saved Games\Missions) or a
-- full Windows path, delay = seconds (default 1) }. The load happens on a later frame, after
-- the ack has gone out; progress arrives as mission_load events.
local pending_load
function commands.load_mission(d)
  local file = tostring(d.file or "")
  if file == "" then error("load_mission needs file") end
  local path = file
  if not file:match("^%a:[/\\]") then path = lfs.writedir() .. "Missions\\" .. file end
  if not lfs.attributes(path) then error("no such mission file " .. path) end
  pending_load = { path = path, at = Sim.getRealTime() + (tonumber(d.delay) or 1) }
  return { path = path }
end

-- data: { player_id?, text }. Without player_id, to everyone.
function commands.chat_to(d)
  if d.player_id then net.send_chat_to(tostring(d.text), tonumber(d.player_id))
  else net.send_chat(tostring(d.text), true) end
end

-- Every connected player: { id, name, ucid, side, slot }.
function commands.players()
  local out = {}
  for _, id in ipairs(net.get_player_list() or {}) do
    local i = net.get_player_info(id) or {}
    out[#out + 1] = { id = id, name = i.name, ucid = i.ucid, side = i.side, slot = i.slot }
  end
  return out
end

-- Move a player to a slot (side 0 and slot "" for spectators). data: { player_id, side, slot }.
function commands.force_slot(d)
  return { ok = net.force_player_slot(tonumber(d.player_id), tonumber(d.side) or 0, tostring(d.slot or "")) }
end

-- Spawn a dynamic-slot aircraft for a player, as the slot screen does for its own player
-- (MultiplayerSelectAirdromeDialog.lua). data: { player_id, spawn = { the dialog's fields } };
-- userId and nickName default to the player. Returns what DCS returned.
function commands.spawn_for(d)
  local spawn = type(d.spawn) == "table" and d.spawn or {}
  local id = tonumber(d.player_id)
  spawn.userId = spawn.userId or id
  spawn.nickName = spawn.nickName or tostring(id)
  if type(spawn.pylon_list) == "table" then  -- JSON can't carry numeric keys: [{num, CLSID}]
    local pylons = {}
    for _, p in ipairs(spawn.pylon_list) do pylons[tonumber(p.num)] = { CLSID = p.CLSID, num = tonumber(p.num) } end
    spawn.pylons, spawn.pylon_list = pylons, nil
  end
  spawn.pylons = spawn.pylons or {}
  spawn.addProps = spawn.addProps or { pylons = spawn.pylons }
  local create = Sim.create_client_aircraft or (DCS and DCS.create_client_aircraft)
  if not create then error("create_client_aircraft is not available here") end
  local ok, res = pcall(create, spawn)
  if not ok then error("create_client_aircraft: " .. tostring(res)) end
  return { result = res }
end

function H.on_message(msg)
  if msg.type ~= "cmd" then return end
  local d = type(msg.data) == "table" and msg.data or {}
  local f = commands[d.op]
  if not f then
    transport.ack(msg.id, false, nil, "unknown op " .. tostring(d.op))
    return
  end
  local ok, res = pcall(f, d)
  if ok then transport.ack(msg.id, true, res) else transport.ack(msg.id, false, nil, tostring(res)) end
end

-- Includes the host: on a listen server the host is a real player. On a dedicated server the
-- server's own player (id 1) never takes a slot, so it produces no slot traffic.
local function is_tracked_player(id)
  return Sim.isServer() and Sim.isMultiplayer() and id ~= nil
end

local function safe(name, f)
  return function(...)
    local ok, res = pcall(f, ...)
    if not ok then log_error(name .. " failed: " .. tostring(res)); return nil end
    return res
  end
end

local callbacks = {}
local next_poll = 0

-- Server frame times, to see what F10 drawings cost (proof P19).
local frames = { n = 0, sum = 0, max = 0, slow = 0, last = nil, since = nil }

local function frame_stats(now)
  if frames.last then
    local dt = now - frames.last
    frames.n, frames.sum = frames.n + 1, frames.sum + dt
    if dt > frames.max then frames.max = dt end
    if dt > 0.05 then frames.slow = frames.slow + 1 end
  end
  frames.last = now
  frames.since = frames.since or now
  if cfg.frame_stats_interval > 0 and now - frames.since >= cfg.frame_stats_interval and frames.n > 0 then
    emit("frame_stats", { frames = frames.n, avg_ms = frames.sum / frames.n * 1000, max_ms = frames.max * 1000,
                          over_50ms = frames.slow, seconds = now - frames.since })
    frames.n, frames.sum, frames.max, frames.slow, frames.since = 0, 0, 0, 0, now
  end
end

callbacks.onSimulationFrame = safe("onSimulationFrame", function()
  if not Sim.isServer() then return end
  local now = Sim.getRealTime()
  frame_stats(now)
  if now < next_poll then return end
  next_poll = now + cfg.poll_interval
  transport.tick()
  if pending_load and now >= pending_load.at then
    local path = pending_load.path
    pending_load = nil
    local ok, res = pcall(net.load_mission, path)
    emit("mission_load", { stage = "requested", path = path, ok = ok and res ~= false, error = not ok and tostring(res) or nil })
  end
end)

local function mission_file()
  local ok, f = pcall(Sim.getMissionFilename)
  return ok and f or nil
end

for cb, stage in pairs({ onMissionLoadBegin = "begin", onMissionLoadEnd = "end",
                         onSimulationStart = "start", onSimulationStop = "stop" }) do
  callbacks[cb] = safe(cb, function()
    if not Sim.isServer() then return end
    emit("mission_load", { stage = stage, file = mission_file(), real_time = Sim.getRealTime() })
  end)
end

callbacks.onPlayerConnect = safe("onPlayerConnect", function(id)
  if not is_tracked_player(id) then return end
  local info = net.get_player_info(id) or {}
  H.ucids[id] = info.ucid
  emit("player_connect", { player_id = id, ucid = info.ucid, name = info.name })
end)

callbacks.onPlayerDisconnect = safe("onPlayerDisconnect", function(id, err_code)
  if not is_tracked_player(id) then return end
  emit("player_disconnect", { player_id = id, ucid = H.ucids[id], reason = err_code })
  H.ucids[id] = nil
end)

local SIDE_NAMES = { [1] = "red", [2] = "blue" }

-- The getAvailableSlots entry for a slot id, with its plain fields only. Fixed slots list
-- unitId (dynamic ones may carry an "_<place>" suffix, see MultiplayerSelectDynamicDialog.lua),
-- type, role, callsign, groupName, and for slots at an airbase airdromeId / helipadId.
local function slot_entry(side, slot)
  if slot == nil or slot == "" then return nil end
  for _, key in ipairs({ tonumber(side), SIDE_NAMES[tonumber(side)] }) do
    local ok, list = pcall(Sim.getAvailableSlots, key)
    if ok and type(list) == "table" then
      for _, v in pairs(list) do
        local uid = tostring(v.unitId)
        if uid == tostring(slot) or uid:match("^(.-)_[^_]*$") == tostring(slot) then
          local e = {}
          for k, x in pairs(v) do
            local t = type(x)
            if t == "string" or t == "number" or t == "boolean" then e[k] = x end
          end
          return e
        end
      end
    end
  end
  return nil
end

-- Chat starting with cfg.chat_prefix is a command for the engine; the other players don't see
-- it. Returning "" drops the message.
callbacks.onPlayerTrySendChat = safe("onPlayerTrySendChat", function(id, msg, all)
  if not is_tracked_player(id) or type(msg) ~= "string" then return nil end
  if msg:sub(1, #cfg.chat_prefix) ~= cfg.chat_prefix then return nil end
  local info = net.get_player_info(id) or {}
  emit("chat_command", { player_id = id, ucid = info.ucid or H.ucids[id], name = info.name, side = info.side,
                         slot = info.slot, text = msg:sub(#cfg.chat_prefix + 1) })
  return ""
end)

-- Return false to deny; return nil (not true) to allow so other hooks still get a say.
callbacks.onPlayerTryChangeSlot = safe("onPlayerTryChangeSlot", function(id, side, slot)
  if not is_tracked_player(id) then return nil end
  local info = net.get_player_info(id) or {}
  local ucid = info.ucid or H.ucids[id]
  local entry = slot_entry(side, slot)
  local unit_type
  if slot and slot ~= "" then pcall(function() unit_type = Sim.getUnitType(slot) end) end
  emit("slot_try", { player_id = id, ucid = ucid, name = info.name, side = side, slot = slot,
                     unit_type = unit_type, entry = entry })
  local reason
  if not H.rules.sides[tonumber(side)] then
    reason = cfg.side_denied_reason
  elseif ucid and H.rules.deny[ucid] then
    reason = H.rules.deny[ucid]
  elseif entry and entry.airdromeId and H.rules.airdromes[tonumber(entry.airdromeId)] then
    reason = H.rules.airdromes[tonumber(entry.airdromeId)]
  end
  if not reason then return nil end
  net.send_chat_to(reason, id)
  emit("slot_denied", { player_id = id, ucid = ucid, name = info.name, side = side, slot = slot, reason = reason })
  return false
end)

callbacks.onPlayerChangeSlot = safe("onPlayerChangeSlot", function(id)
  if not is_tracked_player(id) then return end
  local info = net.get_player_info(id) or {}
  local unit_type
  if info.slot and info.slot ~= "" then
    pcall(function() unit_type = Sim.getUnitType(info.slot) end)
  end
  emit("player_slot", {
    player_id = id, ucid = info.ucid or H.ucids[id], name = info.name,
    side = info.side, slot = info.slot, unit_type = unit_type, entry = slot_entry(info.side, info.slot),
  })
end)

Sim.setUserCallbacks(callbacks)
log_info("campaign hooks loaded (run " .. H.run_id .. ")")

end)

if not loaded then log_error("campaign hooks failed to load: " .. tostring(load_err)) end
