--[[
DCS API probe: answers the open questions behind the campaign bridge. Not part of the campaign.

Normally loaded by build/campaign-probe.miz (see tools/build_test_missions.py), which sets
ProbeConfig and makes one airbase blue with a limited warehouse. Results go to dcs.log, every
line prefixed "[probe]", plus an on-screen summary after ~30 s. Single player is enough.

Without zones, everything happens around the probe airbase (first blue airdrome by default):
test vehicles spawn next to it and the scenery test blows up a building on it. Optional trigger
zones PROBE_SPAWN / PROBE_SCENERY override those locations.
]]

local cfg = {
  airbase = nil,                 -- nil = first blue airdrome
  aircraft_types = {
    -- installed modules
    "FA-18C_hornet", "A-10C_2", "JF-17", "Su-25T", "TF-51D",
    -- not installed: does the warehouse accept them anyway?
    "F-16C_50", "F-15ESE", "AH-64D_BLK_II", "UH-1H", "Mi-8MT",
    -- negative control
    "NOT_A_REAL_TYPE",
  },
  weapon_types = { "weapons.missiles.AIM_120C", "weapons.bombs.GBU_12" },
  ground_types = { "M-1 Abrams", "Hummer" },
  country = "USA",
  socket_host = "127.0.0.1",
  socket_closed_port = 7399,     -- nothing should listen here
  socket_open_port = 7341,       -- run tools/smoke_server.py first to make this one open
  spawn_offset = 600,            -- metres from the airbase reference point
  scenery_radius = 3000,
  known_scenery_id = nil,        -- e.g. the tower id from the terrain's radio.lua ('t:<id>')
  explosion_power = 3000,
}
for k, v in pairs(ProbeConfig or {}) do cfg[k] = v end

local results = {}
local function log(msg) env.info("[probe] " .. msg) end
local function result(item, ok, detail)
  results[#results + 1] = string.format("%s %s: %s", ok and "PASS" or "FAIL", item, detail)
  log(results[#results])
end

local EVENT_NAMES = {}
for name, id in pairs(world.event) do EVENT_NAMES[id] = name end

local function later(delay, f)
  timer.scheduleFunction(function()
    local ok, err = pcall(f)
    if not ok then log("step error: " .. tostring(err)) end
  end, nil, timer.getTime() + delay)
end

local function find_airbase()
  if cfg.airbase then return Airbase.getByName(cfg.airbase) end
  for _, b in ipairs(coalition.getAirbases(coalition.side.BLUE)) do
    if b:getDesc().category == Airbase.Category.AIRDROME then return b end
  end
end

local AIRBASE = find_airbase()
if AIRBASE then log("probe airbase: " .. AIRBASE:getName()) else log("no blue airdrome found") end

-- A zone if the mission has one, otherwise the airbase point (+ offset north).
local function location(zone_name, offset, radius)
  local z = trigger.misc.getZone(zone_name)
  if z then return z.point, z.radius end
  if not AIRBASE then return nil end
  local p = AIRBASE:getPoint()
  return { x = p.x + offset, y = p.y, z = p.z }, radius
end

-- --------------------------------------------------------------------------------------
-- 1. net.lua2json / net.json2lua in the mission environment
-- --------------------------------------------------------------------------------------
local function probe_json()
  if not (net and net.lua2json and net.json2lua) then
    result("1 json", false, "net.lua2json/json2lua not available; bundle a JSON library")
    return
  end
  local sample = {
    int = 1, float = 1.5, neg = -3, big = 1234567890123, bool = true,
    str = 'quote " backslash \\ newline \n unicode é',
    list = { 1, 2, 3 }, empty = {}, nested = { k = "v" }, sparse = { [1] = "a", [3] = "c" },
  }
  local ok, s = pcall(net.lua2json, sample)
  if not ok then result("1 json", false, "lua2json error: " .. tostring(s)) return end
  log("lua2json output: " .. s)
  local ok2, back = pcall(net.json2lua, s)
  if not ok2 then result("1 json", false, "json2lua error: " .. tostring(back)) return end
  local checks = {
    int = back.int == 1, float = back.float == 1.5, big = back.big == 1234567890123,
    str = back.str == sample.str, list = type(back.list) == "table" and #back.list == 3,
    nested = type(back.nested) == "table" and back.nested.k == "v",
    no_raw_newline = s:find("\n", 1, true) == nil,
  }
  local failed = {}
  for k, v in pairs(checks) do if not v then failed[#failed + 1] = k end end
  local empty_as = s:match('"empty":%[%]') and "[]" or (s:match('"empty":{}') and "{}" or "?")
  local null_ok, null_back = pcall(net.json2lua, '{"a":null,"b":[],"c":{}}')
  log("json2lua of null/[]/{}: ok=" .. tostring(null_ok) .. " b type=" .. type(null_ok and null_back.b))
  result("1 json", #failed == 0,
    (#failed == 0 and "round trip ok" or ("mismatched: " .. table.concat(failed, ","))) ..
    "; empty table encodes as " .. empty_as)
end

-- --------------------------------------------------------------------------------------
-- 2. Non-blocking LuaSocket connect (needs require/package unsanitized)
-- --------------------------------------------------------------------------------------
local function probe_socket_target(socket, label, port, done)
  local s = socket.tcp()
  s:settimeout(0)
  local t0 = socket.gettime()
  local r, err = s:connect(cfg.socket_host, port)
  local blocked_ms = (socket.gettime() - t0) * 1000
  log(string.format("%s: connect() returned %s, %s after %.1f ms (frame blocked for that long)",
    label, tostring(r), tostring(err), blocked_ms))
  local start = socket.gettime()
  local function poll()
    local _, w = socket.select(nil, { s }, 0)
    local writable = w and w[1] ~= nil
    local elapsed = socket.gettime() - start
    if writable or r then
      local peer = s:getpeername()
      local r2, err2 = s:connect(cfg.socket_host, port)
      log(string.format("%s: writable after %.2f s; getpeername=%s; second connect()=%s,%s",
        label, elapsed, tostring(peer), tostring(r2), tostring(err2)))
      s:close()
      done(label, blocked_ms, peer ~= nil, elapsed)
    elseif elapsed > 6 then
      local r2, err2 = s:connect(cfg.socket_host, port)
      log(string.format("%s: never writable within 6 s; second connect()=%s,%s",
        label, tostring(r2), tostring(err2)))
      s:close()
      done(label, blocked_ms, false, elapsed)
    else
      later(0.1, poll)
    end
  end
  later(0.1, poll)
end

local function probe_socket()
  if not require or not package then
    result("2 socket", false, "require/package are sanitized in MissionScripting.lua")
    return
  end
  package.path = package.path .. ";.\\LuaSocket\\?.lua"
  package.cpath = package.cpath .. ";.\\LuaSocket\\?.dll"
  local ok, socket = pcall(require, "socket")
  if not ok then result("2 socket", false, "require('socket') failed: " .. tostring(socket)) return end
  local outcomes = {}
  local function done(label, blocked_ms, connected, elapsed)
    outcomes[#outcomes + 1] = string.format("%s connected=%s blocked=%.1fms detect=%.2fs",
      label, tostring(connected), blocked_ms, elapsed)
    if #outcomes == 2 then
      result("2 socket", true, table.concat(outcomes, " | ") ..
        " (expect closed: connected=false, open: connected=true; blocked should be ~0)")
    end
  end
  probe_socket_target(socket, "closed port " .. cfg.socket_closed_port, cfg.socket_closed_port, done)
  probe_socket_target(socket, "open port " .. cfg.socket_open_port, cfg.socket_open_port, done)
end

-- --------------------------------------------------------------------------------------
-- 3. Does Group:destroy() fire DEAD / UNIT_LOST? (baseline: a real kill by explosion)
-- --------------------------------------------------------------------------------------
local seen = {}  -- group name -> list of event names
local watcher = {}
function watcher:onEvent(e)
  -- pcall: an error in an event handler pops a modal dialog that freezes the sim.
  pcall(function()
    local name
    if e.initiator then pcall(function() name = e.initiator:getName() end) end
    name = name and tostring(name)  -- scenery getName() returns a number
    if name and name:match("^probe:") then
      local g = name:match("^(.-)#") or name
      seen[g] = seen[g] or {}
      table.insert(seen[g], EVENT_NAMES[e.id] or tostring(e.id))
    end
  end)
end
world.addEventHandler(watcher)

local function spawn_probe_group(name, p)
  local units = {}
  for i, t in ipairs(cfg.ground_types) do
    units[i] = { name = name .. "#" .. i, type = t, x = p.x + i * 15, y = p.z, heading = 0, skill = "Average" }
  end
  return coalition.addGroup(country.id[cfg.country], Group.Category.GROUND,
    { name = name, task = "Ground Nothing", units = units })
end

local function probe_destroy()
  local p = location("PROBE_SPAWN", cfg.spawn_offset)
  if not p then result("3 destroy", false, "no PROBE_SPAWN zone and no airbase") return end
  local a = spawn_probe_group("probe:destroyed", p)
  local b = spawn_probe_group("probe:killed", { x = p.x + 200, y = 0, z = p.z })
  if not a or not b then result("3 destroy", false, "addGroup failed (check ground_types)") return end
  later(3, function()
    seen["probe:destroyed"], seen["probe:killed"] = {}, {}
    Group.getByName("probe:destroyed"):destroy()
    local u = Unit.getByName("probe:killed#1")
    trigger.action.explosion(u:getPoint(), cfg.explosion_power)
    later(6, function()
      local d = table.concat(seen["probe:destroyed"], ",")
      local k = table.concat(seen["probe:killed"], ",")
      log("events after Group:destroy(): [" .. d .. "]")
      log("events after explosion kill:  [" .. k .. "]")
      result("3 destroy", true, "destroy fired [" .. d .. "]; kill fired [" .. k .. "]")
    end)
  end)
end

-- --------------------------------------------------------------------------------------
-- 4. Warehouse item names for aircraft / weapons
-- --------------------------------------------------------------------------------------
local function probe_warehouse()
  if not AIRBASE then result("4 warehouse", false, "no airbase found") return end
  local wh = AIRBASE:getWarehouse()

  if wh.getInventory then
    local inv = wh:getInventory()
    for section, items in pairs(inv or {}) do
      local keys, n = {}, 0
      for k, v in pairs(items) do
        n = n + 1
        if n <= 40 then keys[#keys + 1] = tostring(k) .. "=" .. tostring(v) end
      end
      log(string.format("inventory[%s]: %d entries; first: %s", tostring(section), n, table.concat(keys, ", ")))
    end
  else
    log("Warehouse:getInventory not available in this DCS version")
  end

  local good, bad = {}, {}
  local function try(item, n)
    local ok, err = pcall(function() wh:setItem(item, n) end)
    local cok, count = pcall(function() return wh:getItemCount(item) end)
    log(string.format("setItem(%q, %d) ok=%s err=%s -> getItemCount=%s",
      item, n, tostring(ok), tostring(err), tostring(cok and count or ("error " .. tostring(count)))))
    if ok and cok and count == n then good[#good + 1] = item else bad[#bad + 1] = item end
  end
  for i, t in ipairs(cfg.aircraft_types) do try(t, 10 + i) end
  for i, t in ipairs(cfg.weapon_types) do try(t, 100 + i) end

  if wh.getInventory then
    local inv = wh:getInventory() or {}
    for _, section in ipairs({ "aircraft", "weapon" }) do
      local keys = {}
      for k, v in pairs(inv[section] or {}) do keys[#keys + 1] = tostring(k) .. "=" .. tostring(v) end
      log(string.format("inventory[%s] after setItem: %s", section, table.concat(keys, ", ")))
    end
  end

  -- setItem accepts any string, so the engine must validate type names itself.
  local known, unknown = {}, {}
  for _, t in ipairs(cfg.aircraft_types) do
    local ok, desc = pcall(Unit.getDescByName, t)
    log(string.format("Unit.getDescByName(%q): ok=%s desc=%s category=%s", t, tostring(ok),
      tostring(ok and desc ~= nil), tostring(ok and desc and desc.category)))
    if ok and desc then known[#known + 1] = t else unknown[#unknown + 1] = t end
  end
  for _, t in ipairs(cfg.weapon_types) do
    local short = t:match("([^%.]+)$")
    for _, name in ipairs({ t, short }) do
      local ok, desc = false, nil
      if Weapon and Weapon.getDescByName then ok, desc = pcall(Weapon.getDescByName, name) end
      log(string.format("Weapon.getDescByName(%q): available=%s ok=%s desc=%s", name,
        tostring(Weapon ~= nil and Weapon.getDescByName ~= nil), tostring(ok), tostring(ok and desc ~= nil)))
    end
  end
  result("4 warehouse", true, "setItem accepted: " .. table.concat(good, ",") ..
    "; setItem rejected: " .. (#bad > 0 and table.concat(bad, ",") or "none") ..
    "; Unit.getDescByName knows: " .. table.concat(known, ",") ..
    "; unknown: " .. (#unknown > 0 and table.concat(unknown, ",") or "none"))
end

-- --------------------------------------------------------------------------------------
-- 5. Scenery object identity: getName() vs id_ vs the DEAD event's initiator
-- --------------------------------------------------------------------------------------
local function box_volume(o)
  local ok, v = pcall(function()
    local b = o:getDesc().box
    return (b.max.x - b.min.x) * (b.max.y - b.min.y) * (b.max.z - b.min.z)
  end)
  return ok and v or 0
end

local function probe_scenery()
  local center, radius = location("PROBE_SCENERY", 0, cfg.scenery_radius)
  if not center then result("5 scenery", false, "no PROBE_SCENERY zone and no airbase") return end
  local found, known = {}, nil
  world.searchObjects(Object.Category.SCENERY,
    { id = world.VolumeType.SPHERE, params = { point = center, radius = math.max(radius or 0, 50) } },
    function(o)
      local p = o:getPoint()
      local f = { obj = o, name = tostring(o:getName()), type = o:getTypeName(), id = o.id_, vol = box_volume(o),
                  dist = math.sqrt((p.x - center.x) ^ 2 + (p.z - center.z) ^ 2) }
      found[#found + 1] = f
      if cfg.known_scenery_id and (tostring(f.name) == tostring(cfg.known_scenery_id)
          or tostring(f.id) == tostring(cfg.known_scenery_id)) then
        known = f
      end
      return true
    end)
  if #found == 0 then result("5 scenery", false, "no scenery found") return end
  table.sort(found, function(a, b) return a.vol > b.vol end)
  log(string.format("%d scenery objects in range; largest:", #found))
  for i = 1, math.min(#found, 15) do
    local f = found[i]
    log(string.format("  getName=%s id_=%s type=%s box=%.0fm3",
      tostring(f.name), tostring(f.id), tostring(f.type), f.vol))
  end
  for _, f in ipairs(found) do
    local t = string.upper(tostring(f.type))
    if t:find("TOWER", 1, true) or t:find("KDP", 1, true) or t:find("ATC", 1, true) then
      log(string.format("  tower-like: getName=%s id_=%s type=%s dist=%.0fm", f.name, tostring(f.id), f.type, f.dist))
    end
  end
  if cfg.known_scenery_id then
    log("known scenery id " .. tostring(cfg.known_scenery_id) .. ": " ..
      (known and ("FOUND as getName=" .. tostring(known.name) .. " id_=" .. tostring(known.id) ..
        " type=" .. tostring(known.type)) or "NOT FOUND by getName or id_"))
  end

  local target = known or found[1]
  local dead_names = {}
  local h = {}
  function h:onEvent(e)
    if e.id == world.event.S_EVENT_DEAD and e.initiator then
      pcall(function()
        if e.initiator:getCategory() == Object.Category.SCENERY then
          dead_names[#dead_names + 1] = tostring(e.initiator:getName()) .. "/id_=" .. tostring(e.initiator.id_)
        end
      end)
    end
  end
  world.addEventHandler(h)
  log("exploding scenery getName=" .. tostring(target.name) .. " type=" .. tostring(target.type))
  trigger.action.explosion(target.obj:getPoint(), cfg.explosion_power)
  later(5, function()
    world.removeEventHandler(h)
    local list = table.concat(dead_names, ", ")
    local matched = list:find(tostring(target.name), 1, true) ~= nil
    result("5 scenery", matched, string.format(
      "target getName=%s id_=%s; DEAD events: [%s]; known id %s",
      tostring(target.name), tostring(target.id), list,
      cfg.known_scenery_id and (known and "matched" or "NOT matched") or "not configured"))
  end)
end

-- --------------------------------------------------------------------------------------

log("starting")
later(1, probe_json)
later(2, probe_socket)
later(3, probe_warehouse)
later(4, probe_destroy)
later(16, probe_scenery)
later(30, function()
  log("==== SUMMARY ====")
  for _, r in ipairs(results) do log(r) end
  trigger.action.outText("Probe finished:\n" .. table.concat(results, "\n"), 60)
end)
