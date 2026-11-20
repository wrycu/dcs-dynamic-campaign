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
  campaignWindow(spec)  show a campaign window (dxgui): spec = { title, lines = [..],
                        buttons = [{key, label}] }. Ctrl+Shift+F9 shows or hides it.
  campaignWeb(arg)      open a web page in DCS (its Chromium widget): arg = { html = } or { url = }.
This client -> server:
  campaignClientHello(version)   on each mission load, so the server knows the script is here.
  campaignClick(key)             a window button (or a page's edQuery "click") was pressed.
  campaignWebEvent(stage, detail)  how the window and web page got on (window, open, created,
                                   loaded, mount_failed).

Lua 5.1 (DCS).
]]

local VERSION = 2

local function log_info(msg) log.write("CAMPAIGN-CLIENT", log.INFO, msg) end
local function log_error(msg) log.write("CAMPAIGN-CLIENT", log.ERROR, msg) end

local ok_load, load_err = pcall(function()

local RPC = require("RPC")
local lfs = lfs or require("lfs")
local Sim = Sim or DCS

local pending_join = false   -- waiting for DCS to give us the slot we asked for

local function from_server(sender_id)
  local server = net.get_server_id and net.get_server_id() or 1
  return Sim.isMultiplayer() and not Sim.isServer() and sender_id == server
end

-- What the stock slot screen does once the slot is ours: net.spawn_player() and unpause the view.
-- (Its UI modules, e.g. MultiplayerSelectRole, are not loadable from Hooks: Run 14c.)
-- Run 14c: works for a mission slot the server moved us into (force_player_slot); for a dynamic
-- slot, DCS logs spawnLocalPlayer but the player is not put in the jet.
local function enter()
  net.spawn_player()
  pcall(function() Sim.setViewPause(false) end)
  log_info("entered: net.spawn_player")
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

-- ---------- P19 part 3: a campaign window (dxgui) and a web page (CEF) inside DCS ----------
-- Built with the stock UI machinery (DialogLoader, the skins the in-mission Esc menu uses), from
-- tables here rather than .dlg files, so the script stays one file.

local HOTKEY = "Ctrl+Shift+F9"   -- shows or hides the campaign window
local FONT = "PTM75F.ttf"        -- the stock in-mission message font (Scripts/UI/GameInfo.dlg)

local function to_server(method, ...)
  RPC.sendEvent(net.get_server_id and net.get_server_id() or 1, method, ...)
end

local function static(x, y, w, h, text, size, color)
  return { type = "Static",
    params = { bounds = { x = x, y = y, w = w, h = h }, text = text or "", enabled = true, visible = true,
               tooltip = "", zindex = 1, angle = 0, pivotPoint = { x = 0, y = 0 } },
    skin = { params = { name = "staticSkin", textWrapping = true },
             states = { released = { [1] = { text = { color = color or "0xffffffff", font = FONT, fontSize = size } } } } } }
end

local function button(x, y, w, h, text)
  return { type = "Button",
    params = { bounds = { x = x, y = y, w = w, h = h }, text = text, enabled = true, visible = true,
               tooltip = "", zindex = 1, tabOrder = 0 },
    skin = { params = { name = "buttonSkinGameMenu" } } }
end

-- spec = { title, lines = { "..." }, buttons = { { key =, label = } } }
local function window_table(spec)
  local W, pad = 440, 12
  local children, y = {}, pad
  children.txtTitle = static(pad, y, W - 2 * pad, 28, spec.title or "Campaign", 20)
  y = y + 34
  for i, line in ipairs(spec.lines or {}) do
    children["txtLine" .. i] = static(pad, y, W - 2 * pad, 22, line, 15, "0xd0d0d0ff")
    y = y + 22
  end
  y = y + 8
  for i, b in ipairs(spec.buttons or {}) do
    children["btnItem" .. i] = button(pad, y, W - 2 * pad, 28, b.label or b.key)
    y = y + 32
  end
  children.btnClose = button(pad, y + 4, 120, 28, "Close (" .. HOTKEY .. ")")
  y = y + 4 + 28 + pad
  return { type = "Window",
    params = { bounds = { x = 60, y = 140, w = W, h = y }, text = "Campaign", enabled = true, draggable = true,
               hasCursor = true, modal = false, offscreen = false, overlay = false, buddy = false,
               resizable = false, clipResizeCursor = false, lockFlow = false, layout = {}, zOrder = 100000 },
    skin = { params = { name = "windowSkin", headerHeight = 0 },
             states = { released = { [1] = { bkg = { center_center = "0x1b2633e6" } } } } },
    children = children }
end

local campaign_window, last_spec = nil, nil

local function show_window(spec, visible)
  local DialogLoader = require("DialogLoader")
  if campaign_window then pcall(function() campaign_window:kill() end) end
  campaign_window = DialogLoader.spawnWidget("dialog", window_table(spec), {}, "campaign_window")
  for i, b in ipairs(spec.buttons or {}) do
    campaign_window["btnItem" .. i]:addChangeCallback(function()
      log_info("window: clicked " .. tostring(b.key))
      to_server("campaignClick", tostring(b.key))
    end)
  end
  campaign_window.btnClose:addChangeCallback(function() campaign_window:setVisible(false) end)
  campaign_window:addHotKeyCallback(HOTKEY, function()
    campaign_window:setVisible(not campaign_window:getVisible())
    log_info("window: hotkey, visible=" .. tostring(campaign_window:getVisible()))
  end)
  campaign_window:setVisible(visible)
  last_spec = spec
end

function RPC.method.campaignWindow(sender_id, spec)
  if not from_server(sender_id) then return end
  local ok, err = pcall(show_window, type(spec) == "table" and spec or {}, true)
  log_info("campaignWindow: " .. (ok and "shown" or ("failed: " .. tostring(err))))
  to_server("campaignWebEvent", "window", ok and "shown" or tostring(err))
end

-- A web page in a window: DCS's own Chromium widget (WebViewWidget, as the Start Here page uses).
-- arg = { html = "..." } or { url = "..." }. The page is written to Saved Games\DCS\Temp\campaign_web
-- and mounted; a url becomes a page that redirects there. Pages can call Lua with
-- window.edQuery({ request = JSON.stringify({ method, parameters }), onSuccess, onFailure }):
-- method "click" sends parameters.key to the server, "close" closes the window.
local web_window, web_view = nil, nil

local function close_web()
  if web_window then
    pcall(function() web_view:destroy() end)
    pcall(function() web_window:kill() end)
  end
  web_window, web_view = nil, nil
end

local function open_web(arg)
  local Window = require("Window")
  local Button = require("Button")
  local Skin = require("Skin")
  local WebViewWidget = require("WebViewWidget")
  close_web()
  local dir = lfs.writedir() .. "Temp\\campaign_web\\"
  lfs.mkdir(lfs.writedir() .. "Temp")
  lfs.mkdir(dir)
  local html = arg.html
  if not html then
    local url = tostring(arg.url or "about:blank")
    html = '<!doctype html><meta charset="utf-8"><p>Loading ' .. url .. ' ...</p>'
        .. '<script>location.replace(' .. net.lua2json(url) .. ')</script>'
  end
  local f = assert(io.open(dir .. "index.html", "wb"))
  f:write(html)
  f:close()

  local W, H, bar = 960, 680, 34
  web_window = Window.new(80, 80, W, H, "Campaign web")
  pcall(function() web_window:setSkin(Skin.getSkin("windowSkin")) end)
  web_window:setDraggable(true)
  web_window:setHasCursor(true)
  web_window:setZOrder(100001)
  local close = Button.new("Close")
  pcall(function() close:setSkin(Skin.getSkin("buttonSkinGameMenu")) end)
  close:setBounds(4, 3, 120, 28)
  close:addChangeCallback(function() close_web() end)
  web_window:insertWidget(close)

  web_view = WebViewWidget.new("CampaignWeb", dir, "index.html")
  web_view:setBounds(0, bar, W, H - bar)
  web_view:browserCreated(function(...)
    log_info("web: browser created"); to_server("campaignWebEvent", "created", "")
  end)
  web_view:onPageLoaded(function(...)
    local info = {}
    for i, v in ipairs({ ... }) do info[i] = tostring(v) end
    log_info("web: page loaded " .. table.concat(info, " "))
    to_server("campaignWebEvent", "loaded", table.concat(info, " "))
  end)
  web_view:onMountFailed(function()
    log_error("web: mount failed for " .. dir); to_server("campaignWebEvent", "mount_failed", dir)
  end)
  web_view:edQueryCallback(function(query_id, json, persistent)
    local ok, req = pcall(net.json2lua, json)
    req = ok and type(req) == "table" and req or {}
    local params = type(req.parameters) == "table" and req.parameters or {}
    log_info("web: query " .. tostring(json))
    if req.method == "click" then
      to_server("campaignClick", tostring(params.key))
      web_view:edQuerySuccess(query_id, '"sent to the server"')
    elseif req.method == "close" then
      web_view:edQuerySuccess(query_id, '"closing"')
      close_web()
    else
      web_view:edQueryFailure(query_id, 501, "unknown method " .. tostring(req.method))
    end
    return true
  end)
  web_window:insertWidget(web_view)
  web_window:addHotKeyCallback("escape", function() close_web() end)
  web_window:setVisible(true)
end

function RPC.method.campaignWeb(sender_id, arg)
  if not from_server(sender_id) then return end
  local ok, err = pcall(open_web, type(arg) == "table" and arg or {})
  log_info("campaignWeb: " .. (ok and "opened" or ("failed: " .. tostring(err))))
  to_server("campaignWebEvent", "open", ok and "ok" or tostring(err))
  if not ok then close_web() end
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
    to_server("campaignClientHello", VERSION)
    -- A hidden window from the start, so the hotkey works before the server sends anything.
    local ok, err = pcall(show_window, last_spec or { title = "Campaign", lines = { "Nothing from the server yet." } }, false)
    if not ok then log_error("window: " .. tostring(err)) end
  end
end

function callbacks.onSimulationStop()
  close_web()
  if campaign_window then pcall(function() campaign_window:kill() end) end
  campaign_window = nil
end

Sim.setUserCallbacks(callbacks)
log_info("campaign client script loaded (version " .. VERSION .. ")")

end)

if not ok_load then log_error("campaign client script failed to load: " .. tostring(load_err)) end
