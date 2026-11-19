--[[
Campaign client script: OPTIONAL, on a player's own PC (proof P19 part 3).

Install (each player who wants one-click joins), then restart DCS:
  Saved Games\DCS\Scripts\Hooks\campaign_client.lua

Why: Run 14 showed the server can create a dynamic-slot aircraft for a player
(DCS.create_client_aircraft), but only the player's own DCS can bring it to life. The stock slot
screen does both halves on the player's PC: it calls DCS.create_client_aircraft and, when DCS
reports the new slot, net.spawn_player(). This script does the same when the campaign server
asks, over the stock RPC channel (Scripts/RPC.lua, as the stock multicrew Hooks use).

It only obeys the server it's connected to, does nothing in single player, and changes nothing
else. Without it, players join through the normal slot list.

RPC methods (server -> this client):
  campaignJoin(spawn)   spawn = the slot screen's fields; pylon_list = [{num, CLSID}] (JSON can't
                        carry sparse numeric keys). userId, nickName and coalition are filled in here.
  campaignEnter()       net.spawn_player() only: enter a slot the server already gave us.
This client -> server:
  campaignClientHello(version)   on each mission load, so the server knows the script is here.

Lua 5.1 (DCS).
]]

local VERSION = 1

local function log_info(msg) log.write("CAMPAIGN-CLIENT", log.INFO, msg) end
local function log_error(msg) log.write("CAMPAIGN-CLIENT", log.ERROR, msg) end

local ok_load, load_err = pcall(function()

local RPC = require("RPC")
local Sim = Sim or DCS

local pending_join = false   -- waiting for DCS to give us the slot we asked for

local function from_server(sender_id)
  local server = net.get_server_id and net.get_server_id() or 1
  return Sim.isMultiplayer() and not Sim.isServer() and sender_id == server
end

-- What the stock screen does once the slot is ours: close the slot screens and spawn.
local function enter()
  local ok = pcall(function() require("MultiplayerSelectRole").onEsc() end)
  if not ok then net.spawn_player() end
  pcall(function() Sim.setViewPause(false) end)
  log_info("entered (" .. (ok and "via the slot screen" or "net.spawn_player") .. ")")
end

function RPC.method.campaignJoin(sender_id, spawn)
  if not from_server(sender_id) then return end
  if type(spawn) ~= "table" then return end
  local me = net.get_my_player_id()
  spawn.userId = me
  spawn.nickName = tostring(me)
  local pylons = {}
  for _, p in ipairs(spawn.pylon_list or {}) do pylons[tonumber(p.num)] = { CLSID = p.CLSID, num = tonumber(p.num) } end
  spawn.pylon_list = nil
  spawn.pylons = pylons
  spawn.addProps = { pylons = pylons }
  pending_join = true
  local res = Sim.create_client_aircraft(spawn)
  log_info("campaignJoin: create_client_aircraft returned " .. tostring(res))
  if res ~= true then pending_join = false end
end

function RPC.method.campaignEnter(sender_id)
  if not from_server(sender_id) then return end
  enter()
end

local callbacks = {}

function callbacks.onPlayerChangeSlot(id)
  if pending_join and id == net.get_my_player_id() then
    pending_join = false
    enter()
  end
end

function callbacks.onMissionLoadEnd()
  pending_join = false
  if Sim.isMultiplayer() and not Sim.isServer() then
    RPC.sendEvent(net.get_server_id and net.get_server_id() or 1, "campaignClientHello", VERSION)
  end
end

Sim.setUserCallbacks(callbacks)
log_info("campaign client script loaded (version " .. VERSION .. ")")

end)

if not ok_load then log_error("campaign client script failed to load: " .. tostring(load_err)) end
