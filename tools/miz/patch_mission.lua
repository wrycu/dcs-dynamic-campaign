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
write_table(params.warehouses_out, "warehouses", warehouses)

print(string.format("patched: %d player group(s), airport %d BLUE, limited aircraft%s", #players, params.airdrome_id,
  (params.dynamic and #params.dynamic > 0) and (", dynamic slots at " .. table.concat(params.dynamic, ", ")) or ""))
