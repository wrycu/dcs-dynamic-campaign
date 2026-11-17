--[[
Reads DCS UnitPayloads files and prints them as JSON on stdout. Called by tools/extract_payloads.py.

  luajit dump_payloads.lua <file.lua> [<file.lua> ...]

Output: {"<unitType>": {"<payload name>": {"pylons": {"<num>": "<CLSID>"}, "tasks": [ids]}}}
Files are loaded with an empty environment; later files override earlier ones per unit type and name.
]]

local function json_str(s)
  return '"' .. s:gsub('[%c"\\]', function(c)
    if c == '"' then return '\\"' elseif c == "\\" then return "\\\\" end
    return string.format("\\u%04x", c:byte())
  end) .. '"'
end

local out = {}
for i = 1, #arg do
  local chunk, err = loadfile(arg[i])
  if not chunk then
    io.stderr:write("skip " .. arg[i] .. ": " .. tostring(err) .. "\n")
  else
    setfenv(chunk, {})
    local ok, t = pcall(chunk)
    if ok and type(t) == "table" and type(t.payloads) == "table" then
      local unit = t.unitType or t.name
      out[unit] = out[unit] or {}
      for _, p in pairs(t.payloads) do
        if type(p) == "table" and p.name and type(p.pylons) == "table" then
          local pylons = {}
          for _, py in pairs(p.pylons) do
            if py.num and py.CLSID then pylons[tonumber(py.num)] = py.CLSID end
          end
          out[unit][p.name] = { pylons = pylons, tasks = p.tasks or {} }
        end
      end
    else
      io.stderr:write("skip " .. arg[i] .. ": not a payload file\n")
    end
  end
end

local parts = {}
for unit, payloads in pairs(out) do
  local ps = {}
  for name, p in pairs(payloads) do
    local pylons = {}
    for num, clsid in pairs(p.pylons) do pylons[#pylons + 1] = json_str(tostring(num)) .. ":" .. json_str(clsid) end
    local tasks = {}
    for _, id in pairs(p.tasks) do tasks[#tasks + 1] = tostring(tonumber(id) or 0) end
    ps[#ps + 1] = json_str(name) .. ':{"pylons":{' .. table.concat(pylons, ",") .. '},"tasks":[' .. table.concat(tasks, ",") .. "]}"
  end
  parts[#parts + 1] = json_str(unit) .. ":{" .. table.concat(ps, ",") .. "}"
end
io.write("{" .. table.concat(parts, ",") .. "}\n")
