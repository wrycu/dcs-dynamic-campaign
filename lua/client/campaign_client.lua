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
  campaignWeb(arg)      open a web page in DCS (its Chromium widget): arg = { html =, mode = mount|data|stock }
                        or { url = }.
  campaignMap(spec)     show the war map (proof P21): a native window with the Mission Editor's map widget,
                        the server's symbols on it and a side panel. Ctrl+Shift+F8 shows or hides it. See
                        open_map for the spec.
This client -> server:
  campaignClientHello(version)   on each mission load, so the server knows the script is here.
  campaignClick(key)             a window button (or a page's edQuery "click") was pressed.
  campaignWebEvent(stage, detail)  how the window and web page got on (window, open, created,
                                   loaded, mount_failed), and the map (map, map_click).

On a server hosted from a player's DCS (MULTIPLAYER > New server), the host is a player too: the
campaign Hooks and this script then run in the same Lua state, and calls between them skip the
network (DCS can't send RPC to itself).

Lua 5.1 (DCS).
]]

local VERSION = 9

local function log_info(msg) log.write("CAMPAIGN-CLIENT", log.INFO, msg) end
local function log_error(msg) log.write("CAMPAIGN-CLIENT", log.ERROR, msg) end

local ok_load, load_err = pcall(function()

local RPC = require("RPC")
local lfs = lfs or require("lfs")
local Sim = Sim or DCS

local pending_join = false   -- waiting for DCS to give us the slot we asked for

local function server_id() return net.get_server_id and net.get_server_id() or 1 end

local function from_server(sender_id)
  if not Sim.isMultiplayer() then return false end
  if Sim.isServer() then return sender_id == net.get_my_player_id() end   -- the host, from its own Hooks
  return sender_id == server_id()
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
local MAP_HOTKEY = "Ctrl+Shift+F8"   -- shows or hides the war map (P21)
local toggle_map, close_map      -- defined with the map, below
local FONT = "PTM75F.ttf"        -- the stock in-mission message font (Scripts/UI/GameInfo.dlg)
local HEADER = 22                -- title bar height

local function to_server(method, ...)
  if Sim.isServer() then   -- the host: the campaign Hooks are in this Lua state
    local f = RPC.method[method]
    if f then
      local ok, err = pcall(f, net.get_my_player_id(), ...)
      if not ok then log_error("to_server " .. method .. ": " .. tostring(err)) end
    end
    return
  end
  RPC.sendEvent(server_id(), method, ...)
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
  -- Run 14k: with headerHeight 0 the window couldn't be dragged; the stock windowSkin's title
  -- bar (as the web window has) is what you drag by.
  local W, pad = 440, 12
  local children, y = {}, HEADER + pad
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
    params = { bounds = { x = 60, y = 140, w = W, h = y }, text = spec.title or "Campaign", enabled = true, draggable = true,
               hasCursor = true, modal = false, offscreen = false, overlay = false, buddy = false,
               resizable = false, clipResizeCursor = false, lockFlow = false, layout = {}, zOrder = 100000 },
    skin = { params = { name = "windowSkin", headerHeight = HEADER },
             states = { released = { [1] = { bkg = { center_center = "0x1b2633e6" } } } } },
    children = children }
end

local campaign_window, last_spec = nil, nil
local shown = false
local hotkey_window = nil

local function set_shown(on)
  if not campaign_window then return end
  campaign_window:setVisible(on)
  shown = on
end

-- Run 14k: a window hidden with setVisible(false) no longer gets its hotkey. Run 27: neither did
-- one shrunk to 0x0 (the map's hotkey never fired before the map was open). So both hotkeys live
-- on a window of their own, 1x1 in the corner, transparent and without a cursor, that stays
-- visible from mission load to the end (as the Scratchpad mod keeps its window).
local function make_hotkey_window()
  if hotkey_window then return end
  local DialogLoader = require("DialogLoader")
  hotkey_window = DialogLoader.spawnWidget("dialog", { type = "Window",
    params = { bounds = { x = 0, y = 0, w = 1, h = 1 }, text = "", enabled = true, draggable = false,
               hasCursor = false, modal = false, offscreen = false, overlay = false, buddy = false,
               resizable = false, clipResizeCursor = false, lockFlow = false, layout = {}, zOrder = 99999 },
    skin = { params = { name = "windowSkin", headerHeight = 0 },
             states = { released = { [1] = { bkg = { center_center = "0x00000000" } } } } },
    children = {} }, {}, "campaign_hotkeys")
  hotkey_window:addHotKeyCallback(HOTKEY, function()
    log_info("hotkey " .. HOTKEY)
    if campaign_window then set_shown(not shown) end
  end)
  hotkey_window:addHotKeyCallback(MAP_HOTKEY, function()
    log_info("hotkey " .. MAP_HOTKEY)
    local ok, err = pcall(toggle_map)
    if not ok then log_error("map hotkey: " .. tostring(err)) end
  end)
  hotkey_window:setVisible(true)
  log_info("hotkeys ready: " .. HOTKEY .. " (window), " .. MAP_HOTKEY .. " (map)")
end

local function show_window(spec, visible)
  local DialogLoader = require("DialogLoader")
  local keep = campaign_window and { campaign_window:getBounds() } or nil
  if campaign_window then pcall(function() campaign_window:kill() end) end
  local t = window_table(spec)
  if keep then t.params.bounds.x, t.params.bounds.y = keep[1], keep[2] end  -- stay where it was dragged
  campaign_window = DialogLoader.spawnWidget("dialog", t, {}, "campaign_window")
  for i, b in ipairs(spec.buttons or {}) do
    campaign_window["btnItem" .. i]:addChangeCallback(function()
      log_info("window: clicked " .. tostring(b.key))
      to_server("campaignClick", tostring(b.key))
    end)
  end
  campaign_window.btnClose:addChangeCallback(function() set_shown(false) end)
  campaign_window.onClose = function() set_shown(false) end   -- the title bar's X
  set_shown(visible)
  last_spec = spec
end

function RPC.method.campaignWindow(sender_id, spec)
  if not from_server(sender_id) then return end
  local ok, err = pcall(show_window, type(spec) == "table" and spec or {}, true)
  log_info("campaignWindow: " .. (ok and "shown" or ("failed: " .. tostring(err))))
  to_server("campaignWebEvent", "window", ok and "shown" or tostring(err))
end

-- A web page in a window: DCS's own Chromium widget (WebViewWidget, as the Start Here page uses).
-- arg = { html = "...", mode = } or { url = "..." }. Modes, to find out what loads (Run 14k: the
-- browser was created but a page mounted from Saved Games never showed):
--   mount  the html is written to Saved Games\DCS\Temp\campaign_web and mounted (forward slashes)
--   data   the html is loaded as a data: URL (Run 14l: never works; DCS loads every page through
--          its own file system: "Can't open file: /data:...")
--   stock  DCS's own Start Here page, the control: if that doesn't show either, the widget
--          doesn't render here at all
--   url    (arg.url) that address is loaded
-- Pages can call Lua with
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
  local mode = arg.url and "url" or tostring(arg.mode or "mount")
  local html = arg.html or '<!doctype html><meta charset="utf-8"><body style="background:#1b2633;color:#fff">blank</body>'
  local dir, file, load_url = (lfs.writedir() .. "Temp/campaign_web/"):gsub("\\", "/"), "index.html", nil
  if mode == "stock" then
    dir = "./Scripts/UI/StartHereMissions/ui/"
  else
    lfs.mkdir(lfs.writedir() .. "Temp")
    lfs.mkdir(dir)
    local f = assert(io.open(dir .. file, "wb"))
    f:write(mode == "mount" and html or '<!doctype html><meta charset="utf-8"><body style="background:#1b2633;color:#fff">loading...</body>')
    f:close()
    if mode == "data" then
      load_url = "data:text/html;charset=utf-8," .. html:gsub("[^%w%-_%.~]", function(c) return string.format("%%%02X", c:byte()) end)
    elseif mode == "url" then
      load_url = tostring(arg.url)
    end
  end
  log_info("web: mode " .. mode .. ", mount " .. dir .. file .. (load_url and (", then load " .. load_url:sub(1, 80)) or ""))

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

  web_view = WebViewWidget.new("CampaignWeb", dir, file)
  web_view:setBounds(0, bar, W, H - bar)
  web_view:browserCreated(function(...)
    log_info("web: browser created"); to_server("campaignWebEvent", "created", mode)
    if load_url then
      local ok, err = pcall(function() web_view:cefLoadUrl(load_url) end)
      log_info("web: cefLoadUrl " .. (ok and "called" or ("failed: " .. tostring(err))))
    end
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

-- ---------- P21: the war map, a native window with the Mission Editor's map widget ----------
-- GUIMapView is the map Instant Action's editor and the in-game DTC manager use: DCS's own terrain
-- map, zoom and pan, the editor's symbols ("user objects", from its classifier) and its drawing
-- layer (lines and filled shapes, as the editor's draw panel makes). The server sends what to show.
--
-- spec = {
--   title, center = { x, y }, scale (metres across, roughly), mode = "map"|"altitude"|"satellite",
--   keep_view = true      redraw without moving the camera (a refresh)
--   show = false          don't open the window: keep the map for the hotkey, or redraw it in
--                         place if it's open (the server's periodic refresh, and the map sent on
--                         joining); default true
--   panel = { "line", ... }   the side panel's text
--   detail_scale = metres  the view's width (east-west, measured on the map) at which objects with
--                         detail = true are drawn too (version 8: measured, not getScale: Run 27)
--   (version 6) any object can be in layers: detail = true (drawn zoomed in), groups = { "f:1", ... }
--   (drawn while one of those is selected), or both (either); without either it's always drawn.
--   select = "f:1" on an icon or line: clicking it selects that group (clicking nothing clears it).
--   flights = { { label, select, info, fit = { x0, y0, x1, y1 } }, ... }   (version 7) the panel's
--                         flight buttons: one selects its group, moves the view to the box's middle
--                         and zooms out if the box doesn't fit (never in: Run 27)
--                         (version 9) name, tail, takeoff_in = seconds: the label is
--                         "<name>  T-h:mm:ss  <tail>", counted down here on DCS model time
--   flights_none = "..."  what the flights block says when there are none (one line)
--   objects = {           all positions in mission metres (x north, y east), colours {r, g, b, a} 0..1
--     { kind = "icon", x, y, sym = SYMBOLS key, color, heading (deg), label, info }
--     { kind = "text", x, y, text, color, fill, size, info }                   (drawing layer)
--     { kind = "line", points = { {x, y}, ... }, color, style = LINE_STYLES key, info }
--     { kind = "area", points, color, fill, style, info }                     (drawing layer)
--     { kind = "ring", x, y, r, color, fill, style, info }                    (drawing layer)
-- (Run 26: the editor's own line symbols, RouteLine and ThreatRangeBorder, didn't show; lines and
-- rings are on the drawing layer.)
--   } }
-- Every step reports to the server (campaignWebEvent "map"), so a probe sees how far it got.

local NM = 1852
local MAP_PANEL_W = 340
local MAP_FLIGHTS = 6      -- flight buttons in the panel
local SYMBOLS = {
  tank = "P0091000001", ifv = "P0091000002", sam_long = "P0091000013", sam_medium = "P0091000012",
  sam_short = "P0091000011", aaa = "P0091000014", fighter = "P0091000024", helo = "P0091000021",
  awacs = "P0091000056", tanker = "P0091000064", airbase = "AirdromeClass1", plane = "IAG_PLANE",
  target = "IAG_TARGET",
  -- version 4: the campaign server's war map
  apc = "P0091000004", truck = "P0091000005", artillery = "P0091000006", mlrs = "P0091000007",
  recon = "P0091000003", hq = "P0091000071", depot = "P0091000072", building = "P0091000076",
  radar = "P0091000036", farp = "P0091000022", attack = "P0091000025", bomber = "P0091000027",
  transport = "P0091000029", uav = "P0091000023",
}
local DRAW_DIR = "./MissionEditor/data/NewMap/images/draw/"
local LINE_STYLES = {   -- the editor's line styles: texture, and its height (the line's thickness)
  solid = { "polyline_solid.png", 8 }, solid2 = { "polyline_Solid2.png", 16 }, dash = { "polyline_dash2.png", 16 },
  dot = { "polyline_dotted2.png", 16 }, boundary = { "polyline_Bound1.png", 32 },
  boundary2 = { "polyline_Bound2.png", 32 }, triangle = { "polyline_triangle2.png", 32 },
  wire = { "polyline_wirefence.png", 32 },
}

local map_window, map_view, map_spec = nil, nil, nil
local map_stale = false    -- map_spec arrived while the window was hidden: draw it when shown
local map_info = {}        -- "u<id>" / "d<id>" -> what a click on that object shows
local map_icons = {}       -- { x, y, info } of each icon, for clicks (our own hit test)
local map_lines = {}       -- { points, info } of each line with info: routes, the front (Run 27:
                           -- findDrawObjects doesn't find polylines)
local map_size = nil       -- the screen size the window was built for
local map_selected = nil   -- the selected group (a flight: its route's air defence is drawn)
local map_zoomed = false   -- zoomed in past spec.detail_scale: detail objects are drawn
local map_vw, map_vh = 1, 1  -- the map view's size, pixels
local map_flights_t = nil  -- DCS model time when map_spec's flights' takeoff_in were counted from
local map_tick = 0         -- os.clock() of the last countdown update
local map_next_id = 2000000

local function map_event(stage, detail)
  log_info("map: " .. stage .. " " .. tostring(detail))
  to_server("campaignWebEvent", stage, tostring(detail))
end

local function nid() map_next_id = map_next_id + 1; return map_next_id end

local function col(c, default)
  c = type(c) == "table" and c or default or { 1, 1, 1, 1 }
  return { tonumber(c[1]) or 1, tonumber(c[2]) or 1, tonumber(c[3]) or 1, tonumber(c[4]) or 1 }
end

local function circle(x, y, r, sides)
  local pts = {}
  for i = 0, sides do
    local a = 2 * math.pi * i / sides
    pts[#pts + 1] = { x = x + r * math.cos(a), y = y + r * math.sin(a) }
  end
  return pts
end

local function pts_of(list)
  local out = {}
  for _, p in ipairs(type(list) == "table" and list or {}) do
    out[#out + 1] = { x = tonumber(p.x or p[1]) or 0, y = tonumber(p.y or p[2]) or 0 }
  end
  return out
end

-- Drawing-layer shapes take points relative to their x, y (me_draw_panel.lua).
local function draw_shape(objectType, pts, o, closed)
  local style = LINE_STYLES[o.style or "solid"] or LINE_STYLES.solid
  local ox, oy = pts[1].x, pts[1].y
  local rel = {}
  for i, p in ipairs(pts) do rel[i] = { x = p.x - ox, y = p.y - oy } end
  local data = { objectType = objectType, points = rel, x = ox, y = oy, thickness = style[2],
                 file = DRAW_DIR .. style[1], color = col(o.color), closed = closed or false }
  if objectType == "Polygon" then data.fillColor = col(o.fill, { 0, 0, 0, 0 }) end
  local id = map_view:createDrawObject(data)
  map_view:addDrawObject(id, true)
  if o.info then map_info["d" .. tostring(id)] = o.info end
end

local function visible(o)
  if o.detail == nil and o.groups == nil then return true end
  if o.detail and map_zoomed then return true end
  if map_selected and type(o.groups) == "table" then
    for _, g in ipairs(o.groups) do if g == map_selected then return true end end
  end
  return false
end

local function draw_objects(objects)
  local user, counts, failed = {}, {}, {}
  local function add_user(obj, info)
    user[#user + 1] = obj
    if info then map_info["u" .. tostring(obj.id)] = info end
  end
  for _, o in ipairs(objects or {}) do
    if visible(o) then
      local ok, err = pcall(function()
        local k = o.kind
        if k == "icon" then
          local c = col(o.color)
          add_user({ classKey = SYMBOLS[o.sym] or o.sym or SYMBOLS.target, id = nid(), x = o.x, y = o.y,
                     angle = tonumber(o.heading) or 0, color = c }, o.info or o.label)
          map_icons[#map_icons + 1] = { x = o.x, y = o.y, info = o.info or o.label, select = o.select }
          if o.label then   -- Run 26: coloured labels were hard to read; white with a dark outline, as Instant Action
            add_user({ classKey = "AirdromeCaption", id = nid(), x = o.x, y = o.y, title = tostring(o.label),
                       color = { 1, 1, 1 }, blurColor = { 0, 0, 0 }, offsetX = 16, offsetY = 0 }, o.info or o.label)
          end
        elseif k == "line" then
          local pts = pts_of(o.points)
          draw_shape("Polyline", pts, o, false)
          if o.info then map_lines[#map_lines + 1] = { points = pts, info = o.info, select = o.select } end
        elseif k == "area" then
          draw_shape("Polygon", pts_of(o.points), o, true)
        elseif k == "ring" then
          draw_shape("Polygon", circle(o.x, o.y, o.r, 48), o, true)
        elseif k == "text" then
          local id = map_view:createDrawObject({ objectType = "TextBox", x = o.x, y = o.y, angle = 0,
            color = col(o.color), text = tostring(o.text or ""), font = "DejaVuLGCSansCondensed.ttf",
            fontSize = tonumber(o.size) or 14, fillColor = col(o.fill, { 0, 0, 0, 0.5 }), borderThickness = 0 })
          map_view:addDrawObject(id, true)
          if o.info then map_info["d" .. tostring(id)] = o.info end
        else
          error("unknown kind " .. tostring(k))
        end
      end)
      counts[o.kind or "?"] = (counts[o.kind or "?"] or 0) + (ok and 1 or 0)
      if not ok then failed[#failed + 1] = tostring(o.kind) .. ": " .. tostring(err) end
    end
  end
  if #user > 0 then map_view:addUserObjects(user) end
  local parts = {}
  for k, n in pairs(counts) do parts[#parts + 1] = k .. " " .. n end
  table.sort(parts)
  return table.concat(parts, ", "), failed
end

local function clear_objects()
  map_info, map_icons, map_lines = {}, {}, {}
  pcall(function() map_view:clearUserObjects() end)
  pcall(function() map_view:clearDrawObjects() end)
end

-- Draw map_spec again for the current zoom and selection (a refresh, a click, crossing the zoom).
local function redraw(why)
  if not (map_view and map_spec) then return end
  local t0 = os.clock()
  clear_objects()
  local drawn, failed = draw_objects(map_spec.objects)
  if why then
    map_event("map", string.format("%s: drawn in %.0f ms: %s%s", why, (os.clock() - t0) * 1000, drawn,
                                    #failed > 0 and ("; FAILED " .. table.concat(failed, "; ")) or ""))
  end
  return drawn, failed
end

local function model_time()
  local ok, t = pcall(function() return Sim.getModelTime() end)
  return ok and tonumber(t) or nil
end

-- T-0:19:32, as BMS shows the time to take-off (T+ once it's past).
local function t_minus(dt)
  local s = math.floor(dt + 0.5)
  local sign = "-"
  if s < 0 then sign, s = "+", -s end
  return string.format("T%s%d:%02d:%02d", sign, math.floor(s / 3600), math.floor(s / 60) % 60, s % 60)
end

local function flight_label(f, now)
  if f.takeoff_in and f.name and map_flights_t and now then
    return tostring(f.name) .. "  " .. t_minus(tonumber(f.takeoff_in) - (now - map_flights_t)) .. "  " .. tostring(f.tail or "")
  end
  return tostring(f.label or f.select)
end

-- The panel's flight buttons, from spec.flights.
local function set_flights(spec)
  local list = type(spec.flights) == "table" and spec.flights or {}
  local now = model_time()
  for i = 1, MAP_FLIGHTS do
    local b, f = map_window["btnFlight" .. i], list[i]
    if b then
      b:setText(f and flight_label(f, now) or "")
      b:setVisible(f ~= nil)
    end
  end
  map_window.txtFlights:setText(#list > 0 and "YOUR FLIGHTS (click one for its plan)"
                                or ("YOUR FLIGHTS: " .. tostring(spec.flights_none or "none")))
end

-- Once a second while the map is open: count the take-offs down.
local function tick_flights()
  if not (map_window and map_spec and map_window:getVisible()) then return end
  if os.clock() - map_tick < 1 then return end
  map_tick = os.clock()
  local list = type(map_spec.flights) == "table" and map_spec.flights or {}
  local now = model_time()
  for i = 1, math.min(#list, MAP_FLIGHTS) do
    local b = map_window["btnFlight" .. i]
    if b then b:setText(flight_label(list[i], now)) end
  end
end

-- What the view shows, in map metres: north-south (map x) and east-west (map y). Run 27: the
-- view's scale isn't metres across, so it's measured.
local function view_span(view)
  local ax, ay = view:getMapPoint(0, 0)
  local bx, by = view:getMapPoint(map_vw, map_vh)
  return math.abs(ax - bx), math.abs(ay - by)
end

local function is_zoomed(view)
  local limit = map_spec and tonumber(map_spec.detail_scale)
  if not limit then return false end
  local _, ew = view_span(view)
  return ew <= limit
end

-- Bring the box { x0, y0, x1, y1 } into view: centre on it, zoom out only if it doesn't fit.
local function bring_into_view(view, box)
  local x0, y0, x1, y1 = tonumber(box[1]), tonumber(box[2]), tonumber(box[3]), tonumber(box[4])
  if not (x0 and y0 and x1 and y1) then return end
  view:setCamera((x0 + x1) / 2, (y0 + y1) / 2)
  local ns, ew = view_span(view)
  local need = math.max((x1 - x0) / math.max(ns, 1), (y1 - y0) / math.max(ew, 1)) / 0.85
  if need > 1 then
    view:setScale(math.min(4000000, view:getScale() * need))
    view:setCamera((x0 + x1) / 2, (y0 + y1) / 2)
  end
end

local function check_zoom(view)
  local z = is_zoomed(view)
  if z ~= map_zoomed then
    map_zoomed = z
    redraw(z and "zoomed in" or "zoomed out")
  end
end

local function line_near(pts, x, y, r)
  for i = 2, #pts do
    local a, b = pts[i - 1], pts[i]
    local dx, dy = b.x - a.x, b.y - a.y
    local len2 = dx * dx + dy * dy
    local t = len2 > 0 and math.max(0, math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / len2)) or 0
    local px, py = a.x + t * dx, a.y + t * dy
    if (px - x) ^ 2 + (py - y) ^ 2 <= r * r then return true end
  end
  return false
end

-- Mouse: drag (any button) pans, the wheel zooms about the cursor, a click shows what's there.
local function setup_mouse(view)
  local state = view:getState()
  local down, dragged = nil, false
  function state:onMouseDown(x, y, button) down, dragged = { x = x, y = y }, false end
  function state:onMouseDrag(dx, dy, button, x, y)
    if math.abs(dx) + math.abs(dy) > 0 then dragged = true end
    local cx, cy = view:getCamera()
    local x0, y0 = view:getMapPoint(0, 0)
    local x1, y1 = view:getMapPoint(dx, dy)
    view:setCamera(cx - (x1 - x0), cy - (y1 - y0))
  end
  function state:onMouseWheel(x, y, clicks)
    local mx, my = view:getMapPoint(x, y)
    local scale = view:getScale() * (clicks > 0 and 0.8 or 1.25) ^ math.abs(clicks)
    view:setScale(math.max(2000, math.min(4000000, scale)))
    local mx2, my2 = view:getMapPoint(x, y)
    local cx, cy = view:getCamera()
    view:setCamera(cx + mx - mx2, cy + my - my2)
    check_zoom(view)
  end
  function state:onMouseUp(x, y, button)
    if dragged or not down then return end
    local mx, my = view:getMapPoint(x, y)
    -- 14 px in metres. Screen right is map east (y), not map x: Run 26's radius came out as zero.
    local ex, ey = view:getMapPoint(x + 14, y)
    local radius = math.sqrt((ex - mx) ^ 2 + (ey - my) ^ 2)
    local found, best, select = nil, radius, nil
    for _, i in ipairs(map_icons) do
      local d = math.sqrt((i.x - mx) ^ 2 + (i.y - my) ^ 2)
      if d <= best then found, best, select = i.info, d, i.select end
    end
    if not found then   -- lines: every one under the cursor (routes overlap near their base)
      local hits = {}
      for _, l in ipairs(map_lines) do
        if line_near(l.points, mx, my, radius * 0.7) and #hits < 3 then
          hits[#hits + 1] = l.info
          select = select or l.select    -- the first route's flight
        end
      end
      if #hits > 0 then found = table.concat(hits, "\n\n") end
    end
    if not found then
      for _, id in ipairs(view:findUserObjects(mx, my, radius) or {}) do
        found = found or map_info["u" .. tostring(id)]
      end
    end
    if not found then
      for _, id in ipairs(view:findDrawObjects(mx, my, radius) or {}) do
        found = found or map_info["d" .. tostring(id)]
      end
    end
    local text = found or string.format("Nothing here (%.0f, %.0f).", mx, my)
    map_window.txtInfo:setText(text)
    map_event("map_click", text)
    -- A flight selects its group (its route's air defence); a click on nothing clears it.
    local sel = select or (found and map_selected) or nil
    if sel ~= map_selected then
      map_selected = sel
      redraw(sel and ("selected " .. sel) or "selection cleared")
    end
  end
end

-- The whole screen: the map on the left, the panel on the right (Run 27: the text, then what was
-- clicked, then the buttons, each a full-width row so their labels fit).
local function map_window_table(W, H)
  local x0, pad, pw = W - MAP_PANEL_W + 10, 6, MAP_PANEL_W - 20
  local bw = math.floor((pw - 2 * pad) / 3)
  local info_h = math.max(200, math.floor(H * 0.25))
  local buttons_y = H - 3 * 34 - 8
  local info_y = buttons_y - info_h - 8
  local flights_y = info_y - 8 - 24 - MAP_FLIGHTS * 32
  local children = {
    txtPanel = static(x0, HEADER + 8, pw, flights_y - HEADER - 16, "", 14, "0xd0d0d0ff"),
    txtFlights = static(x0, flights_y, pw, 22, "YOUR FLIGHTS", 14, "0xd0d0d0ff"),
    txtInfo = static(x0, info_y, pw, info_h, "Click a symbol or a route for details.", 14, "0xffe080ff"),
    btnModeMap = button(x0, buttons_y, bw, 28, "Map"),
    btnModeAlt = button(x0 + bw + pad, buttons_y, bw, 28, "Terrain"),
    btnModeSat = button(x0 + 2 * (bw + pad), buttons_y, bw, 28, "Satellite"),
    btnCenter = button(x0, buttons_y + 34, pw, 28, "Back to the front"),
    btnCloseMap = button(x0, buttons_y + 68, pw, 28, "Close (" .. MAP_HOTKEY .. ")"),
  }
  for i = 1, MAP_FLIGHTS do
    children["btnFlight" .. i] = button(x0, flights_y + 24 + (i - 1) * 32, pw, 28, "")
  end
  return { type = "Window",
    params = { bounds = { x = 0, y = 0, w = W, h = H }, text = "Campaign", enabled = true, draggable = false,
               hasCursor = true, modal = false, offscreen = false, overlay = false, buddy = false,
               resizable = false, clipResizeCursor = false, lockFlow = false, layout = {}, zOrder = 100002 },
    skin = { params = { name = "windowSkin", headerHeight = HEADER },
             states = { released = { [1] = { bkg = { center_center = "0x1b2633f2" } } } } },
    children = children }
end

-- The editor's symbol set, read from its file (me_map_classifier would also load the editor's
-- unit database, me_db_api, for module symbols we don't use).
local function load_classifier()
  local f, err = loadfile("./MissionEditor/data/NewMap/classifier.lua")
  if not f then error("classifier: " .. tostring(err)) end
  return f(function(s) return s end), "classifier.lua"
end

local function frame(spec)
  local c = type(spec.center) == "table" and spec.center or {}
  if c.x and c.y then map_view:setCamera(tonumber(c.x), tonumber(c.y)) end
  map_view:setScale(tonumber(spec.scale) or 300000)
  map_zoomed = is_zoomed(map_view)
end

close_map = function()
  if map_window then
    pcall(function() map_view:destroy() end)
    pcall(function() map_window:kill() end)
  end
  map_window, map_view, map_size = nil, nil, nil
  map_selected, map_zoomed = nil, false
end

local function build_map()
  local Gui = require("dxgui")
  local GUIMapView = require("GUIMapView")
  local DialogLoader = require("DialogLoader")
  local sw, sh = Gui.GetWindowSize()
  local W, H = sw, sh
  map_size = { sw, sh }
  map_event("map", "step window " .. W .. "x" .. H .. " on " .. sw .. "x" .. sh)
  map_window = DialogLoader.spawnWidget("dialog", map_window_table(W, H), {}, "campaign_map")
  map_view = GUIMapView.new()
  map_window:insertWidget(map_view)
  map_view:setBounds(0, HEADER, W - MAP_PANEL_W, H - HEADER)
  map_vw, map_vh = W - MAP_PANEL_W, H - HEADER
  map_view:setBkgColor({ 107 / 255, 175 / 255, 248 / 255 })
  local classifier, source = load_classifier()
  map_view:loadClassifier(classifier)
  map_event("map", "step classifier from " .. source)
  local theme = "nato"
  pcall(function() theme = require("Options.Data").getIconsTheme() or theme end)
  local ok_theme, err_theme = pcall(function() map_view:onIconsThemeChange(theme) end)
  map_event("map", "step icons theme " .. tostring(theme) .. (ok_theme and "" or (" failed: " .. tostring(err_theme))))
  pcall(function() map_view:showLayer("MGRS GRID", false) end)
  pcall(function() map_view:showLayer("GEOGRAPHICAL GRID", false) end)
  local ok_b, err_b = pcall(function()
    local Terrain = require("terrain")
    local sw_b, ne_b = Terrain.GetTerrainConfig("SW_bound"), Terrain.GetTerrainConfig("NE_bound")
    map_view:setMapBounds(sw_b[1] * 1000, sw_b[3] * 1000, ne_b[1] * 1000, ne_b[3] * 1000)
  end)
  map_event("map", "step bounds " .. (ok_b and "set" or ("failed: " .. tostring(err_b))))
  map_view:initEDTerrainGraphics("./")
  map_event("map", "step terrain graphics")
  setup_mouse(map_view)

  local function mode(m) return function()
    local ok, err = pcall(function() map_view:setMapMode(m) end)
    map_event("map", "mode " .. m .. (ok and "" or (" failed: " .. tostring(err))))
  end end
  map_window.btnModeMap:addChangeCallback(mode("map"))
  map_window.btnModeAlt:addChangeCallback(mode("altitude"))
  map_window.btnModeSat:addChangeCallback(mode("satellite"))
  map_window.btnCenter:addChangeCallback(function()
    if map_spec then
      local was = map_zoomed
      frame(map_spec)
      if map_zoomed ~= was then redraw("zoomed out") end
    end
  end)
  for i = 1, MAP_FLIGHTS do
    map_window["btnFlight" .. i]:addChangeCallback(function()
      local f = map_spec and type(map_spec.flights) == "table" and map_spec.flights[i]
      if not f then return end
      map_window.txtInfo:setText(tostring(f.info or f.label or ""))
      if type(f.fit) == "table" then bring_into_view(map_view, f.fit) end
      map_zoomed = is_zoomed(map_view)
      map_selected = f.select
      map_event("map_click", tostring(f.label))
      redraw("selected " .. tostring(f.select))
    end)
  end
  map_window.btnCloseMap:addChangeCallback(function() map_window:setVisible(false) end)
  map_window.onClose = function() map_window:setVisible(false) end
end

-- The screen size changed since the window was built (a resolution change): build it again.
local function screen_changed()
  if not map_size then return false end
  local ok, sw, sh = pcall(function() return require("dxgui").GetWindowSize() end)
  return ok and (sw ~= map_size[1] or sh ~= map_size[2])
end

local function open_map(spec)
  local t0 = os.clock()
  if map_window and not map_window:getVisible() and screen_changed() then close_map() end
  -- show = false and the window isn't on screen: just keep it (the hotkey draws it). A refresh
  -- while it's hidden costs nothing then.
  if spec.flights_t == nil then spec.flights_t = model_time() end   -- when its countdowns start
  if spec.show == false and not (map_window and map_window:getVisible()) then
    map_spec, map_stale = spec, true
    map_event("map", "kept for the hotkey (" .. tostring(type(spec.objects) == "table" and #spec.objects or 0) .. " objects)")
    return
  end
  local fresh = map_window == nil
  if fresh then
    local ok, err = pcall(build_map)
    if not ok then
      map_event("map", "failed building the window: " .. tostring(err))
      close_map()
      return
    end
  end
  map_spec = spec
  pcall(function() map_window:setText(tostring(spec.title or "Campaign")) end)
  map_window.txtPanel:setText(table.concat(type(spec.panel) == "table" and spec.panel or {}, "\n"))
  map_flights_t = spec.flights_t
  set_flights(spec)
  local ok_mode, err_mode = pcall(function() map_view:setMapMode(spec.mode or "map") end)
  if not ok_mode then map_event("map", "mode failed: " .. tostring(err_mode)) end
  if fresh or not spec.keep_view then frame(spec) end
  local drawn, failed = redraw()
  map_stale = false
  map_window:setVisible(true)
  map_event("map", string.format("%s in %.0f ms: %s%s", fresh and "shown" or "redrawn", (os.clock() - t0) * 1000,
                                  drawn, #failed > 0 and ("; FAILED " .. table.concat(failed, "; ")) or ""))
end

toggle_map = function()
  if map_window and not map_window:getVisible() and screen_changed() then
    close_map()
  end
  if map_window and map_window:getVisible() then
    map_window:setVisible(false)
  elseif map_spec and (map_stale or not map_window) then
    -- Draw what came while it was hidden; keep the view the player left, if the window exists.
    local spec = {}
    for k, v in pairs(map_spec) do spec[k] = v end
    spec.show, spec.keep_view = true, map_window ~= nil
    local ok, err = pcall(open_map, spec)
    if not ok then map_event("map", "failed: " .. tostring(err)) end
  elseif map_window then
    map_window:setVisible(true)
  else
    log_info("map: nothing from the server yet")
  end
end

function RPC.method.campaignMap(sender_id, spec)
  if not from_server(sender_id) then return end
  local ok, err = pcall(open_map, type(spec) == "table" and spec or {})
  if not ok then map_event("map", "failed: " .. tostring(err)) end
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
  if Sim.isMultiplayer() then   -- a client, or the host of a server hosted from DCS
    to_server("campaignClientHello", VERSION)
    local ok, err = pcall(make_hotkey_window)
    if not ok then log_error("hotkeys: " .. tostring(err)) end
  end
end

function callbacks.onSimulationFrame()
  pcall(tick_flights)
end

function callbacks.onSimulationStop()
  close_web()
  close_map()
  if campaign_window then pcall(function() campaign_window:kill() end) end
  if hotkey_window then pcall(function() hotkey_window:kill() end) end
  campaign_window, hotkey_window = nil, nil
end

Sim.setUserCallbacks(callbacks)
log_info("campaign client script loaded (version " .. VERSION .. ")")

end)

if not ok_load then log_error("campaign client script failed to load: " .. tostring(load_err)) end
