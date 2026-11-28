--[[
Campaign bridge: mission scripting environment side.

Load once at mission start with a DO SCRIPT FILE trigger. Runs on the dedicated server only
(DCS runs mission scripts on the server in multiplayer). Clients need nothing installed.

Requirements:
  * MissionScripting.lua must leave `require` and `package` unsanitized so LuaSocket can load.
  * The server should not pause when empty: the network pump runs on model time and stops
    while the mission is paused.

Optional: define a global CampaignBridgeConfig table before loading to override `defaults`.

Protocol: see docs/protocol.md. Lua 5.1 (DCS).
]]

if CampaignBridge and CampaignBridge.loaded then
  env.info("[campaign] bridge already loaded; skipping")
  return
end

local defaults = {
  host = "127.0.0.1",
  port = 7341,
  poll_interval = 0.1,       -- seconds of model time between network pumps
  telemetry_interval = 3.0,
  reconnect_interval = 5.0,
  connect_timeout = 5.0,
  max_queue = 5000,          -- unsent sequenced messages kept while disconnected
  retain = 2000,             -- sent messages kept for replay after a reconnect
  max_lines = 200,           -- inbound lines handled per pump
  max_bytes = 256 * 1024,    -- outbound bytes written per pump
  hit_throttle = 1.0,        -- seconds between forwarded hits on the same target
}

local cfg = {}
for k, v in pairs(defaults) do cfg[k] = v end
for k, v in pairs(CampaignBridgeConfig or {}) do cfg[k] = v end

local function log(msg) env.info("[campaign] " .. msg) end
local function log_error(msg) env.error("[campaign] " .. msg) end

package.path = package.path .. ";.\\LuaSocket\\?.lua"
package.cpath = package.cpath .. ";.\\LuaSocket\\?.dll"
local socket = require("socket")

assert(net and net.lua2json and net.json2lua,
  "[campaign] net.lua2json/json2lua unavailable in the mission environment")

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
-- Mission state and helpers
-- ======================================================================================

local B = {
  loaded = true,
  -- Wall clock from LuaSocket: model time and math.random repeat across mission restarts.
  run_id = string.format("%.0f", socket.gettime() * 1000000),
  spawned = {},     -- campaign group name -> true
  dead = {},        -- unit/static/scenery name -> true (dedupe DEAD / UNIT_LOST / CRASH)
  suppressed = {},  -- group name -> true while being despawned (ignore its loss events)
  hit_last = {},    -- target name -> model time of last forwarded hit
  left = {},        -- unit name -> model time a player left it (slot change / disconnect)
  ejected = {},     -- unit name -> true once its pilot ejected
  walkers = {},     -- ejected pilots, followed in telemetry: key -> { obj, from (unit description) }
  menus = {},       -- audience -> { kind, id, path }
  config = cfg,
}
CampaignBridge = B

local function now() return timer.getTime() end

local CATEGORY = { [1] = "unit", [2] = "weapon", [3] = "static", [4] = "base", [5] = "scenery", [6] = "cargo" }
local SIDES = { neutral = 0, red = 1, blue = 2 }
local GROUP_CATEGORY = { airplane = 0, helicopter = 1, ground = 2, ship = 3 }

local function pos2(p) return { x = p.x, y = p.z } end          -- DCS vec3 -> model Pos
local function vec3(p) return { x = p.x, y = 0, z = p.y } end   -- model Pos -> DCS vec3

local function describe(o)
  if not o then return nil end
  local d = {}
  pcall(function()
    local name = o:getName()
    d.name = name ~= nil and tostring(name) or nil  -- scenery getName() returns a number
    d.category = CATEGORY[o:getCategory()]
    d.type = o:getTypeName()
    if o.getCoalition then d.coalition = o:getCoalition() end
    if d.category == "unit" then
      local g = o:getGroup()
      if g then d.group = g:getName() end
      d.player = o:getPlayerName()
    end
    local p = o:getPoint()
    d.pos, d.alt = pos2(p), p.y
  end)
  if d.name and not d.group and d.category == "unit" then
    d.group = d.name:match("^(.-)#%d+$")  -- campaign units encode their group in the name
  end
  return d
end

local function place_name(e)
  if not e.place then return nil end
  local ok, name = pcall(function() return e.place:getName() end)
  return ok and name or nil
end

local function country_id(c)
  if type(c) == "number" then return c end
  local id = country.id[c]
  if not id then error("unknown country " .. tostring(c)) end
  return id
end

local function side_of(name)
  local s = SIDES[name]
  if not s then error("unknown coalition " .. tostring(name)) end
  return s
end

-- "all" | "coalition:<side>" | "group:<group name>"  ->  kind, id, group_name
local function parse_audience(a)
  a = a or "all"
  if a == "all" then return "all", -1 end
  local kind, val = a:match("^(%a+):(.+)$")
  if kind == "coalition" then return "coalition", side_of(val) end
  if kind == "group" then
    local g = Group.getByName(val)
    if not g then error("no such group " .. val) end
    return "group", g:getID(), val
  end
  error("bad audience " .. tostring(a))
end

-- ======================================================================================
-- Transport instance
-- ======================================================================================

local transport = new_transport({
  host = cfg.host, port = cfg.port, src = "mission", socket = socket,
  encode = net.lua2json, decode = net.json2lua, now = now, log = log,
  max_queue = cfg.max_queue, retain = cfg.retain, reconnect_interval = cfg.reconnect_interval,
  connect_timeout = cfg.connect_timeout, max_lines = cfg.max_lines, max_bytes = cfg.max_bytes,
  hello_data = function()
    local spawned = {}
    for name in pairs(B.spawned) do
      local g = Group.getByName(name)
      if g and g:isExist() then spawned[#spawned + 1] = name else B.spawned[name] = nil end
    end
    return { proto = 1, run_id = B.run_id, map = env.mission.theatre, spawned = spawned }
  end,
  on_message = function(msg) B.on_message(msg) end,
})
B.transport = transport

local function emit(kind, data)
  data = data or {}
  data.kind = kind
  transport.send("event", data, now())
end

-- ======================================================================================
-- Events: DCS -> engine
-- ======================================================================================

local E = world.event

-- When a player leaves an aircraft (slot change, spectators, disconnect) DCS removes it and
-- fires PILOT_DEAD + UNIT_LOST/DEAD right after PLAYER_LEAVE_UNIT (seen on DCS 2.9.31).
-- Those are reported as unit_removed, not as a kill. Not applied after an ejection: the
-- abandoned jet then crashes for real.
local LEAVE_WINDOW = 2.0

local function left_recently(name)
  local t = name and B.left[name]
  return t ~= nil and now() - t <= LEAVE_WINDOW
end

-- Unit names are reused (player slots respawn as the same unit), so per-life state resets on birth.
local function reset_life(name)
  if not name then return end
  B.dead[name], B.left[name], B.ejected[name] = nil, nil, nil
end

local function report_loss(o)
  local d = describe(o)
  if not d or not d.name or B.dead[d.name] then return end
  if d.group and B.suppressed[d.group] then return end
  B.dead[d.name] = true
  if left_recently(d.name) then
    emit("unit_removed", { unit = d, reason = "player_left" })
    return
  end
  if d.category == "scenery" then
    emit("scenery_destroyed", { object = d })
  elseif d.category == "unit" or d.category == "static" then
    emit("unit_killed", { unit = d })
  end
end

-- An ejected pilot (the object DCS hands the ejection events) is followed in telemetry, to see
-- whether the server knows where a pilot walking on the ground is (proof P19).
local function follow_walker(o, from)
  local ok, key = pcall(function() return tostring(o.id_ or o:getName()) end)
  if not ok or not key then return end
  local w = B.walkers[key] or { obj = o }
  w.obj = o
  w.from = w.from or from
  B.walkers[key] = w
end

-- F10 map marks players place (to map where rides start, Run 31). Not the campaign's own: those
-- use ids from next_mark up.
local function mark_event(change)
  return function(e)
    if e.idx and e.idx >= 7000000 then return end
    emit("mark", { change = change, idx = e.idx, text = e.text, coalition = e.coalition,
                   pos = e.pos and pos2(e.pos) or nil, author = describe(e.initiator) })
  end
end

local event_map = {
  [E.S_EVENT_BIRTH] = function(e)
    local d = describe(e.initiator)
    if d then reset_life(d.name) end
    if d and d.category == "unit" then emit("birth", { unit = d, place = place_name(e) }) end
    -- A player's group (a Client group built into the session mission for a campaign flight) is
    -- followed in telemetry from its first birth, as if spawned here.
    if d and d.category == "unit" and d.player and d.group then B.spawned[d.group] = true end
  end,
  [E.S_EVENT_TAKEOFF] = function(e) emit("takeoff", { unit = describe(e.initiator), place = place_name(e) }) end,
  [E.S_EVENT_LAND] = function(e) emit("land", { unit = describe(e.initiator), place = place_name(e) }) end,
  [E.S_EVENT_ENGINE_SHUTDOWN] = function(e) emit("engine_shutdown", { unit = describe(e.initiator) }) end,
  [E.S_EVENT_DEAD] = function(e) report_loss(e.initiator) end,
  [E.S_EVENT_UNIT_LOST] = function(e) report_loss(e.initiator) end,
  [E.S_EVENT_CRASH] = function(e) report_loss(e.initiator) end,
  [E.S_EVENT_KILL] = function(e)
    emit("kill", { killer = describe(e.initiator), victim = describe(e.target), weapon = e.weapon_name })
  end,
  [E.S_EVENT_HIT] = function(e)
    local t = describe(e.target)
    if not t or not t.name or (t.category ~= "static" and t.category ~= "scenery") then return end
    local last = B.hit_last[t.name]
    if last and now() - last < cfg.hit_throttle then return end
    B.hit_last[t.name] = now()
    local weapon
    if e.weapon then pcall(function() weapon = e.weapon:getTypeName() end) end
    emit("hit", { target = t, shooter = describe(e.initiator), weapon = weapon })
  end,
  [E.S_EVENT_EJECTION] = function(e)
    local d = describe(e.initiator)
    if d and d.name then B.ejected[d.name] = true end
    local pilot = e.target and describe(e.target) or nil
    if e.target then follow_walker(e.target, d) end
    emit("ejection", { unit = d, pilot = pilot })
  end,
  [E.S_EVENT_PILOT_DEAD] = function(e)
    local d = describe(e.initiator)
    if d and left_recently(d.name) then return end  -- the player left; nobody died
    emit("pilot_dead", { unit = d })
  end,
  [E.S_EVENT_LANDING_AFTER_EJECTION] = function(e)
    if e.initiator then follow_walker(e.initiator, nil) end
    emit("landing_after_ejection", { unit = describe(e.initiator), place = place_name(e) })
  end,
  [E.S_EVENT_MARK_ADDED] = mark_event("added"),
  [E.S_EVENT_MARK_CHANGE] = mark_event("changed"),
  [E.S_EVENT_MARK_REMOVED] = mark_event("removed"),
  [E.S_EVENT_PLAYER_ENTER_UNIT] = function(e) emit("player_enter_unit", { unit = describe(e.initiator) }) end,
  [E.S_EVENT_PLAYER_LEAVE_UNIT] = function(e)
    local d = describe(e.initiator)
    if d and d.name and not B.ejected[d.name] and not B.dead[d.name] then B.left[d.name] = now() end
    emit("player_leave_unit", { unit = d })
  end,
}

local handler = {}
function handler:onEvent(e)
  local f = event_map[e.id]
  if not f then return end
  local ok, err = pcall(f, e)
  if not ok then log_error("event " .. tostring(e.id) .. " failed: " .. tostring(err)) end
end
world.addEventHandler(handler)

-- ======================================================================================
-- Commands: engine -> DCS
-- ======================================================================================

-- Engine shape id -> the DCS mark id currently showing it. Every draw uses a fresh DCS id and
-- removes the old mark afterwards. On a dedicated server (Run 8), shapes drawn once reached
-- clients but shapes redrawn by removing and re-adding the same id did not. Fresh ids also
-- stop the flicker between remove and add.
local marks = {}
local next_mark = 7000000  -- well above the ids DCS gives marks players place themselves

local function remove_shape(id)
  local real = marks[id]
  marks[id] = nil
  trigger.action.removeMark(real or id)
end

local function draw_new(d, id)
  local kind, aud = parse_audience(d.audience)
  local color = d.color or { 1, 1, 1, 1 }
  local fill = d.fill or { 0, 0, 0, 0 }
  local line_type = d.line_type or 1
  local read_only = d.read_only ~= false
  local message = d.message or ""

  local shape = d.shape
  if shape == "mark" then
    if kind == "all" then
      trigger.action.markToAll(id, d.text or "", vec3(d.pos), read_only, message)
    elseif kind == "coalition" then
      trigger.action.markToCoalition(id, d.text or "", vec3(d.pos), aud, read_only, message)
    else
      trigger.action.markToGroup(id, d.text or "", vec3(d.pos), aud, read_only, message)
    end
    return
  end
  if kind == "group" then error("shape " .. tostring(shape) .. " cannot target a group") end
  if shape == "line" then
    trigger.action.lineToAll(aud, id, vec3(d.from), vec3(d.to), color, line_type, read_only, message)
  elseif shape == "circle" then
    trigger.action.circleToAll(aud, id, vec3(d.center), d.radius, color, fill, line_type, read_only, message)
  elseif shape == "rect" then
    trigger.action.rectToAll(aud, id, vec3(d.from), vec3(d.to), color, fill, line_type, read_only, message)
  elseif shape == "text" then
    trigger.action.textToAll(aud, id, vec3(d.pos), color, fill, d.font_size or 14, read_only, d.text or "")
  elseif shape == "poly" then
    local args = { 7, aud, id }
    for _, p in ipairs(d.points or {}) do args[#args + 1] = vec3(p) end
    if #args < 6 then error("poly needs at least 3 points") end
    args[#args + 1] = color
    args[#args + 1] = fill
    args[#args + 1] = line_type
    args[#args + 1] = read_only
    args[#args + 1] = message
    trigger.action.markupToAll(unpack(args))
  else
    error("unknown shape " .. tostring(shape))
  end
end

local function draw_shape(d)
  if type(d.id) ~= "number" then error("draw needs a numeric id") end
  next_mark = next_mark + 1
  draw_new(d, next_mark)
  local old = marks[d.id]
  marks[d.id] = next_mark
  if old then trigger.action.removeMark(old) end
end

local function menu_selected(arg)
  local ok, err = pcall(emit, "menu_select", { key = arg.key, audience = arg.audience, group = arg.group })
  if not ok then log_error("menu_select failed: " .. tostring(err)) end
end

local function menu_add_sub(kind, id, label, parent)
  if kind == "all" then return missionCommands.addSubMenu(label, parent) end
  if kind == "coalition" then return missionCommands.addSubMenuForCoalition(id, label, parent) end
  return missionCommands.addSubMenuForGroup(id, label, parent)
end

local function menu_add_cmd(kind, id, label, parent, arg)
  if kind == "all" then return missionCommands.addCommand(label, parent, menu_selected, arg) end
  if kind == "coalition" then return missionCommands.addCommandForCoalition(id, label, parent, menu_selected, arg) end
  return missionCommands.addCommandForGroup(id, label, parent, menu_selected, arg)
end

local function menu_remove(m)
  pcall(function()
    if m.kind == "all" then missionCommands.removeItem(m.path)
    elseif m.kind == "coalition" then missionCommands.removeItemForCoalition(m.id, m.path)
    else missionCommands.removeItemForGroup(m.id, m.path) end
  end)
end

local function menu_build(kind, id, parent, items, audience, gname)
  for _, it in ipairs(items or {}) do
    if it.items then
      local sub = menu_add_sub(kind, id, it.label, parent)
      menu_build(kind, id, sub, it.items, audience, gname)
    else
      menu_add_cmd(kind, id, it.label, parent, { key = it.key, audience = audience, group = gname })
    end
  end
end

local function unit_state(u)
  local d = describe(u)
  pcall(function()
    local p = u:getPosition()
    local hdg = math.deg(math.atan2(p.x.z, p.x.x))
    if hdg < 0 then hdg = hdg + 360 end
    d.hdg = hdg
    local v = u:getVelocity()
    d.spd = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
  end)
  return d
end

local commands = {}

function commands.spawn_group(d)
  local cat = GROUP_CATEGORY[d.category]
  if not cat then error("unknown group category " .. tostring(d.category)) end
  local gt = d.group
  if type(gt) ~= "table" or not gt.name then error("spawn_group needs group.name") end
  local existing = Group.getByName(gt.name)
  if existing and existing:isExist() then error("group " .. gt.name .. " already exists") end
  for _, u in ipairs(gt.units or {}) do
    if u.name then B.dead[u.name] = nil end  -- names are reused across respawns
    if type(u.callsign) == "table" then
      -- Western callsign {1=name index, 2=flight, 3=unit, name=...}: JSON made the numeric keys strings.
      local cs = {}
      for k, v in pairs(u.callsign) do cs[tonumber(k) or k] = v end
      u.callsign = cs
    end
    local pl = u.payload
    if type(pl) == "table" then
      -- JSON object keys arrive as strings; DCS wants pylons keyed by pylon number.
      local pylons = {}
      for k, v in pairs(pl.pylons or {}) do pylons[tonumber(k) or k] = v end
      pl.pylons = pylons
      if pl.fuel_fraction ~= nil then
        local desc = Unit.getDescByName(u.type)
        if desc and desc.fuelMassMax then pl.fuel = pl.fuel_fraction * desc.fuelMassMax end
        pl.fuel_fraction = nil
      end
    end
  end
  local g = coalition.addGroup(country_id(d.country), cat, gt)
  if not g then error("addGroup failed for " .. gt.name) end
  local units = {}
  for _, u in ipairs(g:getUnits() or {}) do
    if u:isExist() then units[#units + 1] = u:getName() end
  end
  if #units == 0 then
    -- DCS creates the group but no aircraft when a ramp/runway start's airbase warehouse is short of the type.
    pcall(function() g:destroy() end)
    local t = gt.units and gt.units[1] and gt.units[1].type or "?"
    error("DCS created no units for " .. gt.name .. " (for a ramp or runway start: is there " .. t ..
      " in the airbase warehouse?)")
  end
  B.spawned[g:getName()] = true
  return { group = g:getName(), units = units }
end

function commands.despawn_group(d)
  local g = Group.getByName(d.group)
  B.spawned[d.group] = nil
  if not g or not g:isExist() then return { survivors = {}, gone = true } end
  local survivors, alive, pos = {}, {}, nil
  local air = g:getCategory() == Group.Category.AIRPLANE or g:getCategory() == Group.Category.HELICOPTER
  local fuel, ammo, state = nil, {}, nil
  for _, u in ipairs(g:getUnits()) do
    if u:isExist() and u:getLife() > 0 then
      local t = u:getTypeName()
      survivors[t] = (survivors[t] or 0) + 1
      alive[#alive + 1] = u:getName()
      if not pos then
        pos = pos2(u:getPoint())
        if air then state = unit_state(u) end
      end
      if air then
        local f = u:getFuel()
        if f and (not fuel or f < fuel) then fuel = f end
        for _, a in ipairs(u:getAmmo() or {}) do
          local wt = a.desc and a.desc.typeName or "?"
          ammo[wt] = (ammo[wt] or 0) + (a.count or 0)
        end
      end
    end
  end
  local name = d.group
  -- Group:destroy() fired no events on DCS 2.9.31; suppression is a guard in case that changes.
  B.suppressed[name] = true
  g:destroy()
  timer.scheduleFunction(function() B.suppressed[name] = nil end, nil, now() + 5)
  local res = { survivors = survivors, alive = alive, pos = pos }
  if air then
    res.fuel, res.ammo = fuel, ammo
    if state then res.alt, res.hdg, res.spd = state.alt, state.hdg, state.spd end
  end
  return res
end

function commands.spawn_static(d)
  local st = d.static
  if type(st) ~= "table" or not st.name then error("spawn_static needs static.name") end
  B.dead[st.name] = nil
  local o = coalition.addStaticObject(country_id(d.country), st)
  if not o then error("addStaticObject failed for " .. st.name) end
  return { name = o:getName() }
end

function commands.remove_static(d)
  local o = StaticObject.getByName(d.name)
  if o and o:isExist() then
    B.dead[d.name] = true  -- removal is not a kill
    o:destroy()
    return { removed = true }
  end
  return { removed = false }
end

local function controller_of(name)
  local g = Group.getByName(name)
  if not g or not g:isExist() then error("no such group " .. tostring(name)) end
  return g:getController()
end

function commands.set_route(d)
  controller_of(d.group):setTask({ id = "Mission", params = { route = { points = d.points } } })
end

function commands.set_command(d)
  controller_of(d.group):setCommand(d.command)
end

function commands.set_task(d)
  local c = controller_of(d.group)
  if d.push then c:pushTask(d.task) else c:setTask(d.task) end
end

function commands.draw(d) draw_shape(d) end

-- Many shapes in one command: { items = [draw data, ...] }. Returns how many, and how long DCS
-- took (ms of real time), to measure what the F10 map can carry.
-- The mission environment has no `os`; LuaSocket gives a wall clock.
local function wall_clock() return (socket and socket.gettime and socket.gettime()) or (os and os.clock()) or 0 end

function commands.draw_batch(d)
  local t0 = wall_clock()
  local n = 0
  for _, item in ipairs(d.items or {}) do
    draw_shape(item)
    n = n + 1
  end
  return { drawn = n, ms = (wall_clock() - t0) * 1000 }
end

local SMOKE = { green = 0, red = 1, white = 2, orange = 3, blue = 4 }

-- Coloured smoke on the ground at pos. DCS smoke lasts about 5 minutes.
function commands.smoke(d)
  local color = SMOKE[d.color or "red"]
  if not color then error("unknown smoke color " .. tostring(d.color)) end
  local p = { x = d.pos.x, y = 0, z = d.pos.y }
  p.y = land.getHeight({ x = p.x, y = p.z })
  trigger.action.smoke(p, color)
end

function commands.undraw(d)
  local ids = d.ids or { d.id }
  for _, id in ipairs(ids) do remove_shape(id) end
end

function commands.message(d)
  local kind, id = parse_audience(d.audience)
  local dur, clear = d.duration or 10, d.clear == true
  if kind == "all" then trigger.action.outText(d.text, dur, clear)
  elseif kind == "coalition" then trigger.action.outTextForCoalition(id, d.text, dur, clear)
  else trigger.action.outTextForGroup(id, d.text, dur, clear) end
end

-- Replaces the menu tree rooted at d.root for this audience. Empty items removes it.
-- Coalition and "all" menus cannot tell who clicked; use group menus for per-player actions.
function commands.menu(d)
  local audience = d.audience or "all"
  local old = B.menus[audience]
  if old then menu_remove(old); B.menus[audience] = nil end
  if not d.items or #d.items == 0 then return end
  local kind, id, gname = parse_audience(audience)
  local root = menu_add_sub(kind, id, d.root or "Campaign", nil)
  B.menus[audience] = { kind = kind, id = id, path = root }
  menu_build(kind, id, root, d.items, audience, gname)
end

function commands.warehouse_set(d)
  local ab = Airbase.getByName(d.airbase)
  if not ab then error("no such airbase " .. tostring(d.airbase)) end
  local wh = ab:getWarehouse()
  -- setItem accepts any string (unknown names land in the weapon section), and
  -- Unit.getDescByName returns a desc even for bogus names, but with category 4 rather than
  -- 0 (airplane) / 1 (helicopter). Verified on DCS 2.9.31. Weapons cannot be validated here.
  local unknown = {}
  for item in pairs(d.aircraft or {}) do
    local ok, desc = pcall(Unit.getDescByName, item)
    if not (ok and desc and (desc.category == 0 or desc.category == 1)) then unknown[#unknown + 1] = item end
  end
  if #unknown > 0 then error("unknown aircraft type(s): " .. table.concat(unknown, ", ")) end
  for item, count in pairs(d.aircraft or {}) do wh:setItem(item, count) end
  for item, count in pairs(d.weapons or {}) do wh:setItem(item, count) end
  for liquid, amount in pairs(d.liquids or {}) do wh:setLiquidAmount(tonumber(liquid), amount) end
end

function commands.query(d)
  if d.what == "time" then
    return { model = timer.getTime(), abs = timer.getAbsTime() }
  elseif d.what == "group" then
    local g = Group.getByName(d.group)
    if not g or not g:isExist() then return { exists = false } end
    local units = {}
    for _, u in ipairs(g:getUnits()) do
      local st = unit_state(u)
      units[#units + 1] = { name = u:getName(), type = u:getTypeName(), life = u:getLife(), pos = pos2(u:getPoint()),
                            alt = u:getPoint().y, hdg = st.hdg, spd = st.spd }
    end
    return { exists = true, id = tonumber(g:getID()), units = units }
  elseif d.what == "airbase" then
    local ab = Airbase.getByName(d.airbase)
    if not ab then return { exists = false } end
    local p = ab:getPoint()
    return { exists = true, name = ab:getName(), id = tonumber(ab:getID()), pos = pos2(p), alt = p.y,
             coalition = ab:getCoalition(), category = ab:getDesc().category }
  elseif d.what == "parking" then
    -- Parking spots; with free_only, only those DCS considers available right now.
    local ab = Airbase.getByName(d.airbase)
    if not ab then error("no airbase " .. tostring(d.airbase)) end
    local spots = {}
    for _, s in ipairs(ab:getParking(d.free_only and true or false) or {}) do
      spots[#spots + 1] = { term = s.Term_Index, type = s.Term_Type, pos = pos2(s.vTerminalPos),
                            dist_rw = s.fDistToRW, occupied = s.TO_AC and true or false }
    end
    return { spots = spots }
  elseif d.what == "type_size" then
    -- An aircraft type's size from its bounding box (metres): span, length, height.
    local desc = Unit.getDescByName(d.type)
    if not desc or not desc.box then error("no description for type " .. tostring(d.type)) end
    local b = desc.box
    return { span = b.max.z - b.min.z, length = b.max.x - b.min.x, height = b.max.y - b.min.y,
             fuel_max = desc.fuelMassMax }
  elseif d.what == "client_groups" then
    -- The mission's groups with player (Client) seats, as built into the .miz: the campaign's
    -- session mission has one per committed flight. Slot ids are the units' unitIds; x, y and heading
    -- (radians) are where each is parked in the .miz.
    local out = {}
    for _, side in pairs((env.mission or {}).coalition or {}) do
      for _, c in pairs(side.country or {}) do
        for _, cat in ipairs({ "plane", "helicopter" }) do
          for _, g in pairs((c[cat] or {}).group or {}) do
            local units, clients = {}, false
            for _, u in pairs(g.units or {}) do
              units[#units + 1] = { name = u.name, id = tostring(u.unitId), skill = u.skill, type = u.type,
                                    x = u.x, y = u.y, heading = u.heading }
              if u.skill == "Client" then clients = true end
            end
            if clients then out[#out + 1] = { name = g.name, units = units } end
          end
        end
      end
    end
    return { groups = out }
  elseif d.what == "warehouse" then
    -- Aircraft stock at an airbase, for the given types.
    local ab = Airbase.getByName(d.airbase)
    if not ab then error("no airbase " .. tostring(d.airbase)) end
    local wh = ab:getWarehouse()
    local counts = {}
    for _, t in ipairs(d.types or {}) do counts[t] = wh:getItemCount(t) end
    return { counts = counts }
  elseif d.what == "mission" then
    -- The loaded mission's date, start time and weather as DCS has them, and the clock now.
    local m = env.mission or {}
    local w = m.weather or {}
    local wind = w.wind or {}
    local clouds = w.clouds or {}
    return {
      abs = timer.getAbsTime(), model = timer.getTime(), theatre = m.theatre,
      date = m.date, start_time = m.start_time,
      weather = {
        preset = clouds.preset, base = clouds.base, temperature = w.season and w.season.temperature,
        qnh = w.qnh, visibility = w.visibility and w.visibility.distance, fog = w.enable_fog,
        wind_ground = wind.atGround, wind_2000 = wind.at2000, wind_8000 = wind.at8000,
      },
    }
  elseif d.what == "airbases" then
    -- Every airbase on the map (airdromes, helipads/FARPs, ships), with runways and parking.
    local out = {}
    for _, ab in ipairs(world.getAirbases() or {}) do
      local e = {}
      pcall(function()
        local p = ab:getPoint()
        e.name, e.id, e.pos, e.alt = ab:getName(), tonumber(ab:getID()), pos2(p), p.y
        e.category, e.coalition = ab:getDesc().category, ab:getCoalition()
        e.type = ab:getTypeName()
        local rws = {}
        for _, r in ipairs(ab:getRunways() or {}) do
          rws[#rws + 1] = { name = r.Name, course = r.course, length = r.length, width = r.width,
                            pos = r.position and pos2(r.position) or nil }
        end
        e.runways = rws
        e.parking = #(ab:getParking() or {})
      end)
      out[#out + 1] = e
    end
    return { airbases = out }
  elseif d.what == "ll_to_xy" then
    -- Latitude/longitude (degrees) to map x/y, with DCS's own projection.
    local out = {}
    for i, q in ipairs(d.points or {}) do
      local v = coord.LLtoLO(q.lat, q.lon, 0)
      out[i] = { x = v.x, y = v.z }
    end
    return { points = out }
  elseif d.what == "scenery" then
    -- Scenery objects within radius (m) of center: count per type name, and the positions of
    -- those whose type name contains any of d.match (case-insensitive), up to d.limit.
    local counts, found, total = {}, {}, 0
    local limit = d.limit or 500
    local match = {}
    for _, m in ipairs(d.match or {}) do match[#match + 1] = string.lower(m) end
    local vol = { id = world.VolumeType.SPHERE,
                  params = { point = { x = d.center.x, y = land.getHeight({ x = d.center.x, y = d.center.y }),
                                       z = d.center.y }, radius = d.radius } }
    world.searchObjects(Object.Category.SCENERY, vol, function(o)
      local ok, t = pcall(o.getTypeName, o)
      t = ok and t or "?"
      total = total + 1
      counts[t] = (counts[t] or 0) + 1
      if #found < limit and #match > 0 then
        local lt = string.lower(t)
        for _, m in ipairs(match) do
          if string.find(lt, m, 1, true) then
            local pok, p = pcall(o.getPoint, o)
            local nok, name = pcall(o.getName, o)   -- scenery: its map object id, as text
            found[#found + 1] = { type = t, pos = pok and pos2(p) or nil, id = nok and name or nil }
            break
          end
        end
      end
      return true
    end)
    return { total = total, counts = counts, found = found }
  elseif d.what == "footprints" then
    -- Scenery, statics and units within radius (m) of center, each with its footprint on the
    -- ground: the four corners of its bounding box (getDesc().box) turned as it stands, or none if
    -- DCS gives no box (most scenery: the engine then uses the model's box from the terrain's files,
    -- campaign/model_boxes.py, turned by fwd and right, its axes on the ground). For a clear spot to start the ride to the jets (Run 31: inside a terminal).
    -- d.scenery_only: scenery alone; d.min_size (m): only objects whose box is at least this long
    -- or wide (none without a box, unless d.keep_unboxed), to survey an airfield's buildings (tools/airfield_buildings.py).
    local found = {}
    local limit = d.limit or 1500
    local min_size = tonumber(d.min_size) or 0
    local vol = { id = world.VolumeType.SPHERE,
                  params = { point = { x = d.center.x, y = land.getHeight({ x = d.center.x, y = d.center.y }),
                                       z = d.center.y }, radius = d.radius } }
    local cats = d.scenery_only and { Object.Category.SCENERY }
                 or { Object.Category.SCENERY, Object.Category.STATIC, Object.Category.UNIT }
    world.searchObjects(cats, vol, function(o)
      if #found >= limit then return false end
      local e = {}
      local big = min_size <= 0
      pcall(function()
        e.type = o:getTypeName()
        local P = o:getPosition()
        e.pos = pos2(P.p)
        e.fwd, e.right = { x = P.x.x, y = P.x.z }, { x = P.z.x, y = P.z.z }   -- its axes on the ground
        local desc = o:getDesc()
        local b = desc and desc.box
        if b and b.min and b.max then
          local corners = {}
          for _, c in ipairs({ { b.min.x, b.min.z }, { b.max.x, b.min.z }, { b.max.x, b.max.z }, { b.min.x, b.max.z } }) do
            corners[#corners + 1] = { x = P.p.x + P.x.x * c[1] + P.z.x * c[2],
                                      y = P.p.z + P.x.z * c[1] + P.z.z * c[2] }
          end
          e.box = corners
          e.height = b.max.y - b.min.y
          e.length, e.width = b.max.x - b.min.x, b.max.z - b.min.z
          if math.max(e.length, e.width) >= min_size then big = true end
        end
        if d.scenery_only then e.id = tostring(o:getName()) end   -- the map object's id
      end)
      if not e.box and d.keep_unboxed then big = true end   -- most scenery has no box (Run 32)
      if e.pos and big then found[#found + 1] = e end
      return true
    end)
    return { found = found, total = #found }
  elseif d.what == "objects" then
    -- Units, weapons, statics and cargo within radius (m) of center, up to d.limit (50): to find
    -- objects the events don't hand over, e.g. a pilot walking after ejecting.
    local found = {}
    local limit = d.limit or 50
    local vol = { id = world.VolumeType.SPHERE,
                  params = { point = { x = d.center.x, y = land.getHeight({ x = d.center.x, y = d.center.y }),
                                       z = d.center.y }, radius = d.radius } }
    local cats = { Object.Category.UNIT, Object.Category.WEAPON, Object.Category.STATIC, Object.Category.CARGO }
    world.searchObjects(cats, vol, function(o)
      if #found < limit then
        local x = describe(o) or {}
        pcall(function() x.id = o.id_ end)
        found[#found + 1] = x
      end
      return true
    end)
    return { found = found }
  elseif d.what == "road_point" then
    -- Nearest point on the road network ("roads" or "railroads") to d.pos.
    local x, y = land.getClosestPointOnRoads(d.road or "roads", d.pos.x, d.pos.y)
    if not x then return { found = false } end
    return { found = true, pos = { x = x, y = y } }
  elseif d.what == "road_path" then
    -- DCS's own road route from d.from to d.to, as a polyline of {x, y}; empty if there is none.
    local pts = land.findPathOnRoads(d.road or "roads", d.from.x, d.from.y, d.to.x, d.to.y) or {}
    local out = {}
    for i, q in ipairs(pts) do out[i] = { x = q.x, y = q.y } end
    return { points = out }
  elseif d.what == "surface_grid" then
    -- land.getSurfaceType on a grid, as one digit per cell (1 land, 2 shallow water, 3 water,
    -- 4 road, 5 runway), row by row: rows go north (x0 + i*cell), cells east (y0 + j*cell).
    local rows = {}
    for i = 0, d.nx - 1 do
      local cells = {}
      for j = 0, d.ny - 1 do
        cells[j + 1] = tostring(land.getSurfaceType({ x = d.x0 + i * d.cell, y = d.y0 + j * d.cell }) or 0)
      end
      rows[i + 1] = table.concat(cells)
    end
    return { rows = rows }
  elseif d.what == "clear_spots" then
    -- For each of d.points (a vehicle's spot), the nearest spot within d.max m with nothing in a
    -- circle of d.radius m: no scenery (buildings), statics or units, land or road under it (no
    -- water or runway), not steep, and at least d.spacing m from the spots already given. Rings
    -- outward, finer near the point. { none = true } where there is none.
    local r, max_m, spacing = d.radius or 20, d.max or 1000, d.spacing or 15
    local cats = { Object.Category.SCENERY, Object.Category.STATIC, Object.Category.UNIT }
    local taken, out = {}, {}
    local function clear(x, y)
      for _, q in ipairs(taken) do
        if (q.x - x) ^ 2 + (q.y - y) ^ 2 < spacing * spacing then return false end
      end
      local lo, hi = math.huge, -math.huge
      for k = 0, 4 do
        local px, py = x, y
        if k > 0 then px, py = x + r * math.cos(k * math.pi / 2), y + r * math.sin(k * math.pi / 2) end
        local s = land.getSurfaceType({ x = px, y = py })
        if s ~= land.SurfaceType.LAND and s ~= land.SurfaceType.ROAD then return false end
        local h = land.getHeight({ x = px, y = py })
        if h < lo then lo = h end
        if h > hi then hi = h end
      end
      if hi - lo > 4 then return false end
      local hit = false
      local vol = { id = world.VolumeType.SPHERE,
                    params = { point = { x = x, y = land.getHeight({ x = x, y = y }), z = y }, radius = r } }
      world.searchObjects(cats, vol, function() hit = true; return false end)
      return not hit
    end
    local function find(cx, cy)
      if clear(cx, cy) then return { x = cx, y = cy } end
      local ring, tried = 0, 0
      while tried < 1500 do
        local step = ring < 100 and 10 or (ring < 300 and 25 or 50)
        ring = ring + step
        if ring > max_m then break end
        local n = math.max(6, math.ceil(2 * math.pi * ring / step))
        local a0 = ring / step   -- turn each ring a little so the spots don't line up
        for k = 0, n - 1 do
          local a = a0 + 2 * math.pi * k / n
          local x, y = cx + ring * math.cos(a), cy + ring * math.sin(a)
          tried = tried + 1
          if clear(x, y) then return { x = x, y = y } end
        end
      end
      return nil
    end
    for i, q in ipairs(d.points or {}) do
      local spot = find(q.x, q.y)
      if spot then taken[#taken + 1] = spot end
      out[i] = spot or { none = true }   -- the encoder has no null
    end
    return { spots = out }
  elseif d.what == "heights" then
    -- Terrain elevation (m) for each {x, y} in d.points, in order; e.g. for waypoint elevations.
    local out = {}
    for i, p in ipairs(d.points or {}) do out[i] = land.getHeight({ x = p.x, y = p.y }) end
    return { heights = out }
  end
  error("unknown query " .. tostring(d.what))
end

function B.on_message(msg)
  if msg.type ~= "cmd" then return end
  local d = type(msg.data) == "table" and msg.data or {}
  local f = commands[d.op]
  if not f then
    transport.ack(msg.id, false, nil, "unknown op " .. tostring(d.op))
    return
  end
  local ok, res = pcall(f, d)
  if ok then transport.ack(msg.id, true, res) else
    local err = res
    if type(res) == "table" then  -- DCS sometimes raises a table rather than a string
      local jok, js = pcall(net.lua2json, res)
      err = jok and ("error table " .. tostring(js)) or tostring(res)
    end
    log_error((d.op or "?") .. " failed: " .. tostring(err))
    transport.ack(msg.id, false, nil, tostring(err))
  end
end

-- ======================================================================================
-- Telemetry and main loop
-- ======================================================================================


local function telemetry()
  local players, groups = {}, {}
  for _, side in ipairs({ coalition.side.RED, coalition.side.BLUE }) do
    for _, u in ipairs(coalition.getPlayers(side) or {}) do
      if u:isExist() then players[#players + 1] = unit_state(u) end
    end
  end
  for name in pairs(B.spawned) do
    local g = Group.getByName(name)
    if g and g:isExist() then
      local u = g:getUnit(1)
      local entry = { id = name, pos = u and pos2(u:getPoint()) or nil, alive = g:getSize() }
      local cat = g:getCategory()
      if u and (cat == Group.Category.AIRPLANE or cat == Group.Category.HELICOPTER) then
        local st = unit_state(u)
        entry.alt, entry.hdg, entry.spd, entry.fuel = st.alt, st.hdg, st.spd, u:getFuel()
        entry.in_air = u:inAir()
      end
      groups[#groups + 1] = entry
    else
      B.spawned[name] = nil
    end
  end
  local walkers = {}
  for key, w in pairs(B.walkers) do
    local alive = false
    pcall(function() alive = w.obj:isExist() end)
    local d = alive and describe(w.obj) or {}
    d.key, d.exists, d.from = key, alive, w.from
    walkers[#walkers + 1] = d
    if not alive then B.walkers[key] = nil end
  end
  transport.send_volatile("telemetry", { players = players, groups = groups, walkers = walkers }, now())
end

local next_telemetry = 0
local function tick(_, t)
  local ok, err = pcall(function()
    if transport.state == "ready" and t >= next_telemetry then
      next_telemetry = t + cfg.telemetry_interval
      telemetry()
    end
    transport.tick()
  end)
  if not ok then log_error("tick failed: " .. tostring(err)) end
  return t + cfg.poll_interval
end

timer.scheduleFunction(tick, nil, timer.getTime() + 1)
log("bridge loaded (run " .. B.run_id .. ")")
