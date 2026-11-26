--[[
Patches a DCS mission's `mission` and `warehouses` files for the test missions.
Called by tools/build_test_missions.py; runs under luajit (Lua 5.1).

  luajit patch_mission.lua <params.lua>

params.lua returns a table:
  mission_in, mission_out, warehouses_in, warehouses_out  file paths
  loader          Lua source run by a MISSION START "DO SCRIPT" trigger
  comment         trigger comment
  airdrome_id     warehouse airport to make BLUE with limited aircraft (munitions unlimited)
  player_x, player_y  where to move the player group (DCS map coords), or nil
  player_alt      altitude for the player group's units and waypoints (m), or nil
  clients         true: player aircraft become Client slots (for a multiplayer server)
  date            { Year = , Month = , Day = }, or nil
  start_time      seconds after midnight (local), or nil
  weather         a table merged into mission.weather (only the keys given change), or nil
  dynamic         list of airdrome ids: each becomes BLUE with dynamic slots (players pick an
                  aircraft there, from the warehouse stock, which starts empty), hot start allowed
  parked_client   { name =, airdrome_id =, x =, y = }: add a one-ship Client copy of the first player
                  group, parked cold at that airdrome (DCS picks the spot), or nil
  client_groups   list of { name =, airdrome_id =, x =, y =, start = "hot" or "cold" (default hot),
                  waypoints = { { x =, y =, alt = }, ... },
                  units = { { name =, skill =, dtc = <cartridge name> or nil }, ... } }: groups cloned from
                  the first player group, parked at that airdrome (DCS picks the spots), engines
                  running (hot) or not (cold). Their airdromes get unlimited aircraft, or DCS greys
                  the slots out for want of stock. A unit
                  with skill "Client" is a player slot, any other skill is AI; ``dtc`` names a cartridge
                  in the .miz's DTC/ folder that loads itself when the unit spawns. Or nil.
  combined_arms   { slots =, units = { { name =, type =, x =, y =, heading = }, ... } }: that many
                  blue Tactical Commander and JTAC/Operator slots, and one-unit blue ground groups
                  players can control (playerCanDrive), or nil
]]

local params = dofile(arg[1])

local function load_table(path, global)
  local env = {}
  local chunk = assert(loadfile(path))
  setfenv(chunk, env)
  chunk()
  return assert(env[global], path .. " does not define " .. global)
end

-- Serializer in the Mission Editor's layout: ["key"] = value, tab-indented.
local function sorted_keys(t)
  local keys = {}
  for k in pairs(t) do keys[#keys + 1] = k end
  table.sort(keys, function(a, b)
    local ta, tb = type(a), type(b)
    if ta ~= tb then return ta == "number" end
    return a < b
  end)
  return keys
end

local function fmt_value(v)
  local t = type(v)
  if t == "string" then return string.format("%q", v) end
  if t == "boolean" then return tostring(v) end
  if t == "number" then
    if v == math.floor(v) and math.abs(v) < 1e15 then return string.format("%d", v) end
    return string.format("%.17g", v)
  end
  error("cannot serialize " .. t)
end

local function serialize(out, v, indent)
  if type(v) ~= "table" then out[#out + 1] = fmt_value(v) return end
  local pad = string.rep("\t", indent)
  out[#out + 1] = "\n" .. pad .. "{\n"
  for _, k in ipairs(sorted_keys(v)) do
    local key = type(k) == "number" and ("[" .. fmt_value(k) .. "]") or ("[" .. string.format("%q", k) .. "]")
    out[#out + 1] = pad .. "\t" .. key .. " = "
    serialize(out, v[k], indent + 1)
    out[#out + 1] = ",\n"
  end
  out[#out + 1] = pad .. "}"
end

local function write_table(path, global, t)
  local out = { global .. " = " }
  serialize(out, t, 0)
  out[#out + 1] = " -- end of " .. global .. "\n"
  local f = assert(io.open(path, "wb"))
  f:write(table.concat(out))
  f:close()
end

-- ---------- mission ----------

local mission = load_table(params.mission_in, "mission")

-- One MISSION START trigger running the loader. trigrules is what the Mission Editor shows;
-- trig is the compiled form the game executes. Both must agree.
mission.trigrules = {
  [1] = {
    predicate = "triggerStart",
    comment = params.comment,
    eventlist = "",
    rules = {},
    actions = {
      [1] = {
        predicate = "a_do_script", text = params.loader,
        seconds = 10, zone = "", meters = 1000, readonly = false, start_delay = 0,
      },
    },
  },
}
mission.trig = {
  actions = { [1] = "a_do_script(" .. string.format("%q", params.loader) .. ");" },
  conditions = { [1] = "return(true)" },
  funcStartup = { [1] = "if mission.trig.conditions[1]() then mission.trig.actions[1]() end" },
  func = {},
  flag = { [1] = true },
  custom = {},
  customStartup = {},
  events = {},
}

local function player_groups()
  local found = {}
  for _, side in pairs(mission.coalition or {}) do
    for _, ctry in pairs(side.country or {}) do
      for _, cat in ipairs({ "plane", "helicopter" }) do
        for _, g in pairs((ctry[cat] or {}).group or {}) do
          for _, u in pairs(g.units or {}) do
            if u.skill == "Client" or u.skill == "Player" then found[#found + 1] = g break end
          end
        end
      end
    end
  end
  return found
end

local players = player_groups()
assert(#players > 0, "template has no Client/Player aircraft")
if params.clients then
  for _, g in ipairs(players) do
    for _, u in pairs(g.units) do
      if u.skill == "Player" then u.skill = "Client" end
    end
  end
end
if params.player_x and params.player_y then
  for _, g in ipairs(players) do
    local u1 = g.units[1]
    local dx, dy = params.player_x - u1.x, params.player_y - u1.y
    g.x, g.y = (g.x or u1.x) + dx, (g.y or u1.y) + dy
    for _, u in pairs(g.units) do
      u.x, u.y = u.x + dx, u.y + dy
      if params.player_alt then u.alt = params.player_alt end
    end
    for _, p in pairs(((g.route or {}).points) or {}) do
      p.x, p.y = p.x + dx, p.y + dy
      if params.player_alt then p.alt = params.player_alt end
    end
  end
end

-- A one-ship Client group parked cold at an airdrome, cloned from the first player group (P19:
-- the server force-moves a player into it and the client script spawns them). No parking spot is
-- given, so DCS picks a free one when the player spawns.
local function deep_copy(t)
  if type(t) ~= "table" then return t end
  local c = {}
  for k, v in pairs(t) do c[k] = deep_copy(v) end
  return c
end
local function max_ids()
  local gmax, umax = 0, 0
  for _, side in pairs(mission.coalition or {}) do
    for _, ctry in pairs(side.country or {}) do
      for _, cat in pairs(ctry) do
        if type(cat) == "table" and type(cat.group) == "table" then
          for _, g in pairs(cat.group) do
            gmax = math.max(gmax, tonumber(g.groupId) or 0)
            for _, u in pairs(g.units or {}) do umax = math.max(umax, tonumber(u.unitId) or 0) end
          end
        end
      end
    end
  end
  return gmax, umax
end
local pc = params.parked_client
if pc then
  local src = players[1]
  local ctry
  for _, side in pairs(mission.coalition) do
    for _, c in pairs(side.country or {}) do
      for _, g in pairs((c.plane or {}).group or {}) do if g == src then ctry = c end end
    end
  end
  assert(ctry, "parked_client: the first player group is not a plane group")
  local gmax, umax = max_ids()
  local g = deep_copy(src)
  local u = g.units[1]
  g.units = { [1] = u }
  g.groupId, u.unitId = gmax + 1, umax + 1
  g.name, u.name = pc.name, pc.name .. "-1"
  g.x, g.y, u.x, u.y = pc.x, pc.y, pc.x, pc.y
  u.alt, u.speed, u.heading, u.skill = 0, 0, 0, "Client"
  u.parking, u.parking_id = nil, nil
  g.uncontrolled, g.lateActivation = false, nil
  g.route = { points = { [1] = {
    alt = 0, alt_type = "BARO", action = "From Parking Area", type = "TakeOffParking",
    airdromeId = pc.airdrome_id, x = pc.x, y = pc.y, speed = 0, ETA = 0, ETA_locked = true,
    formation_template = "", speed_locked = true,
    task = { id = "ComboTask", params = { tasks = {} } },
  } } }
  local list = ctry.plane.group
  list[#list + 1] = g
end

-- Client groups for planned flights (P20): seats are Client slots or AI, each Client unit with its
-- own cartridge (unit.DTC, as the Mission Editor writes it; the .dtc file is in the .miz's DTC/).
for gi, cg in ipairs(params.client_groups or {}) do
  local src = players[1]
  local ctry
  for _, side in pairs(mission.coalition) do
    for _, c in pairs(side.country or {}) do
      for _, g in pairs((c.plane or {}).group or {}) do if g == src then ctry = c end end
    end
  end
  assert(ctry, "client_groups: the first player group is not a plane group")
  local gmax, umax = max_ids()
  local g = deep_copy(src)
  local proto = g.units[1]
  g.units = {}
  for i, spec in ipairs(cg.units) do
    local u = deep_copy(proto)
    u.unitId, u.name, u.skill = umax + i, spec.name, spec.skill
    u.x, u.y, u.alt, u.speed, u.heading = cg.x, cg.y, 0, 0, 0
    u.parking, u.parking_id = nil, nil
    if type(u.callsign) == "table" then u.callsign[2], u.callsign[3], u.callsign.name = gi + 1, i, nil end
    u.onboard_num = string.format("%03d", (gi + 1) * 10 + i)
    u.DTC = spec.dtc and { AutoLoad = true, Cartridges = { [1] = { name = spec.dtc, default = true } } } or nil
    g.units[i] = u
  end
  g.groupId, g.name = gmax + 1, cg.name
  g.x, g.y = cg.x, cg.y
  g.uncontrolled, g.lateActivation = false, nil
  local hot = (cg.start or "hot") == "hot"
  assert(hot or cg.start == "cold", "client_groups: start must be hot or cold")
  local points = { [1] = {
    alt = 0, alt_type = "BARO",
    action = hot and "From Parking Area Hot" or "From Parking Area",
    type = hot and "TakeOffParkingHot" or "TakeOffParking",
    airdromeId = cg.airdrome_id, x = cg.x, y = cg.y, speed = 0, ETA = 0, ETA_locked = true,
    formation_template = "", speed_locked = true,
    task = { id = "ComboTask", params = { tasks = {} } },
  } }
  for _, wp in ipairs(cg.waypoints or {}) do
    points[#points + 1] = {
      alt = wp.alt, alt_type = "BARO", action = "Turning Point", type = "Turning Point",
      x = wp.x, y = wp.y, speed = 220, ETA = 0, ETA_locked = false, formation_template = "",
      speed_locked = true, task = { id = "ComboTask", params = { tasks = {} } },
    }
  end
  g.route = { points = points }
  local list = ctry.plane.group
  list[#list + 1] = g
end

-- Combined Arms for blue (P19 part 4: a soldier the player walks, whose position the server knows):
-- open Tactical Commander and JTAC/Operator slots for blue, and add one blue soldier players can control.
local ca = params.combined_arms
if ca then
  local gc = mission.groundControl or {}
  mission.groundControl = gc
  gc.roles = gc.roles or {}
  gc.passwords = gc.passwords or {}
  for _, role in ipairs({ "artillery_commander", "forward_observer" }) do
    gc.roles[role] = gc.roles[role] or { neutrals = 0, blue = 0, red = 0 }
    gc.roles[role].blue = ca.slots
    gc.passwords[role] = gc.passwords[role] or {}
  end
  local ctry
  for _, side in pairs(mission.coalition) do
    for _, c in pairs(side.country or {}) do
      for _, g in pairs((c.plane or {}).group or {}) do if g == players[1] then ctry = c end end
    end
  end
  assert(ctry, "combined_arms: no country for the first player group")
  local gmax, umax = max_ids()
  ctry.vehicle = ctry.vehicle or { group = {} }
  ctry.vehicle.group = ctry.vehicle.group or {}
  local list = ctry.vehicle.group
  for i, s in ipairs(ca.units) do
    list[#list + 1] = {
      groupId = gmax + i, name = s.name, x = s.x, y = s.y, visible = false, hidden = false,
      tasks = {}, task = "Ground Nothing", uncontrollable = false, start_time = 0,
      units = { [1] = {
        unitId = umax + i, name = s.name .. "-1", type = s.type, x = s.x, y = s.y, heading = s.heading or 0,
        skill = "Average", playerCanDrive = true,
      } },
      route = { points = { [1] = {
        x = s.x, y = s.y, alt = 0, alt_type = "BARO", type = "Turning Point", action = "Off Road",
        speed = 0, ETA = 0, ETA_locked = true, formation_template = "", speed_locked = true,
        task = { id = "ComboTask", params = { tasks = {} } },
      } } },
    }
  end
end

local function merge(dst, src)
  for k, v in pairs(src) do
    if type(v) == "table" and type(dst[k]) == "table" then merge(dst[k], v) else dst[k] = v end
  end
end
if params.date then mission.date = params.date end
if params.start_time then mission.start_time = params.start_time end
if params.weather then
  mission.weather = mission.weather or {}
  merge(mission.weather, params.weather)
end

write_table(params.mission_out, "mission", mission)

-- ---------- warehouses ----------

local warehouses = load_table(params.warehouses_in, "warehouses")
local ap = warehouses.airports and warehouses.airports[params.airdrome_id]
assert(ap, "warehouses has no airport " .. tostring(params.airdrome_id))
ap.coalition = "BLUE"
ap.unlimitedAircrafts = false
ap.unlimitedMunitions = true  -- AI ramp starts take weapons from stock; we don't track munitions yet
for _, id in ipairs(params.dynamic or {}) do
  local dap = warehouses.airports[id]
  assert(dap, "warehouses has no airport " .. tostring(id))
  dap.coalition = "BLUE"
  dap.unlimitedAircrafts = false
  dap.unlimitedMunitions = true
  dap.aircrafts = {}  -- empty stock; the engine sets it at runtime
  dap.dynamicSpawn = true
  dap.allowHotStart = true
end
-- A Client slot at an airdrome with limited aircraft and none of its type in stock is greyed out in
-- the slot list (Run 25), so airdromes with client groups get unlimited aircraft.
for _, cg in ipairs(params.client_groups or {}) do
  local cap = warehouses.airports[cg.airdrome_id]
  assert(cap, "warehouses has no airport " .. tostring(cg.airdrome_id))
  cap.unlimitedAircrafts = true
end
write_table(params.warehouses_out, "warehouses", warehouses)

print(string.format("patched: %d player group(s), airport %d BLUE, limited aircraft%s%s", #players, params.airdrome_id,
  (params.dynamic and #params.dynamic > 0) and (", dynamic slots at " .. table.concat(params.dynamic, ", ")) or "",
  (pc and string.format(", parked Client %q at airdrome %d", pc.name, pc.airdrome_id) or "")
  .. (ca and string.format(", Combined Arms: %d blue slots, %d drivable units", ca.slots, #ca.units) or "")))
