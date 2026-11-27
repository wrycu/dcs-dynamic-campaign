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
  campaignEnter()       net.spawn_player() only: enter a slot the server already gave us (the war
                        map's Fly button; the map closes).
  campaignWindow(spec)  show a campaign window (dxgui): spec = { title, lines = [..],
                        buttons = [{key, label}] }. Ctrl+Shift+F9 shows or hides it.
  campaignWeb(arg)      open a web page in DCS (its Chromium widget): arg = { html =, mode = mount|data|stock }
                        or { url = }.
  campaignRide(spec), campaignRidePos(p), campaignRideEye(eye), campaignRideStop(why)
                        P22: the camera rides in a vehicle the server drives to the jet (see ride_frame).
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

local VERSION = 24

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
local close_war_map   -- the war map's own close (below): Fly closes it
local stop_ride      -- P22's ride to the jet (below): getting in ends it
local function enter()
  if stop_ride then pcall(stop_ride, "entered") end
  net.spawn_player()
  pcall(function() Sim.setViewPause(false) end)
  if close_war_map then pcall(close_war_map) end
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

-- A filled strip (the scale bar).
local function bar(x, y, w, h, color)
  return { type = "Static",
    params = { bounds = { x = x, y = y, w = w, h = h }, text = "", enabled = true, visible = true,
               tooltip = "", zindex = 1, angle = 0, pivotPoint = { x = 0, y = 0 } },
    skin = { params = { name = "staticSkin" },
             states = { released = { [1] = { bkg = { center_center = color } } } } } }
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
--   packages = { ["f:1"] = "pkg_1", ... }   (version 10) each selectable flight's package: the
--                         list marks the selected flight "> " and its package's other flights "+ "
--   joins = { ["f:1"] = { label, key, ok }, ... }   (version 14) the join button while that flight
--                         is selected: pressing it sends campaignClick(key) ("join:f:1", "leave:f:1");
--                         ok = false greys it out (full, taken off)
--   actions = { ["f:1"] = { label, key, ok, opens_in, ready }, ... }   (version 15) the button under
--                         it for a player seated in that flight: Commit ("commit:f:1", the lead) or
--                         Fly ("fly:f:1"). opens_in = seconds: greyed out until then (counted down
--                         on DCS model time, like takeoff_in), then enabled as ready
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
local AUTO_OPEN_MAP = true -- open the map when this player takes a blue slot (Run 27 ask)
local SCALE_NICE = { 1, 2, 5, 10, 20, 25, 50, 100, 200, 500 }   -- scale bar lengths, nm
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

local tick_flights   -- the flight list's labels (below)
local update_join    -- the join button (below)

-- Draw map_spec again for the current zoom and selection (a refresh, a click, crossing the zoom).
local function redraw(why)
  if not (map_view and map_spec) then return end
  local t0 = os.clock()
  clear_objects()
  local drawn, failed = draw_objects(map_spec.objects)
  if map_window then map_tick = -1e9; pcall(tick_flights); pcall(update_join) end   -- they follow the selection
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

-- "> " the selected flight, "+ " a flight of its package, else two spaces.
local function mark(f)
  if not map_selected or not f.select then return "   " end
  if f.select == map_selected then return ">  " end
  local pk = map_spec and type(map_spec.packages) == "table" and map_spec.packages or {}
  if pk[map_selected] and pk[map_selected] == pk[f.select] then return "+  " end
  return "   "
end

local function flight_label(f, now)
  local text
  if f.takeoff_in and f.name and map_flights_t and now then
    text = tostring(f.name) .. "  " .. t_minus(tonumber(f.takeoff_in) - (now - map_flights_t)) .. "  " .. tostring(f.tail or "")
  else
    text = tostring(f.label or f.select)
  end
  return mark(f) .. text
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
  map_window.txtFlights:setText(#list > 0 and "UPCOMING FLIGHTS"
                                or ("UPCOMING FLIGHTS: " .. tostring(spec.flights_none or "none")))
end

-- Once a second while the map is open: count the take-offs down.
-- The join button (spec.joins) and the one under it (spec.actions: Commit or Fly), shown while a
-- flight players can fly is selected. A Fly that isn't open yet opens here when its time comes.
local function action_of(j, now)
  if type(j) ~= "table" then return nil, false end
  if j.opens_in and map_flights_t and now and tonumber(j.opens_in) - (now - map_flights_t) <= 0 then
    return tostring(j.ready or j.label or ""), j.key ~= nil and j.key ~= ""
  end
  return tostring(j.label or ""), j.ok == true
end

local function show_button(b, j, now)
  local label, ok = action_of(j, now)
  if not label then b:setVisible(false) return end
  b:setText(label)
  b:setEnabled(ok)
  b:setVisible(true)
end

update_join = function()
  if not map_window then return end
  local joins = map_spec and type(map_spec.joins) == "table" and map_spec.joins or {}
  local acts = map_spec and type(map_spec.actions) == "table" and map_spec.actions or {}
  local now = model_time()
  show_button(map_window.btnJoin, map_selected and joins[map_selected], now)
  show_button(map_window.btnAct, map_selected and acts[map_selected], now)
end

tick_flights = function()
  if not (map_window and map_spec and map_window:getVisible()) then return end
  if os.clock() - map_tick < 1 then return end
  map_tick = os.clock()
  pcall(update_scale)   -- also after a button's fit or "Back to the front"
  local list = type(map_spec.flights) == "table" and map_spec.flights or {}
  local now = model_time()
  for i = 1, math.min(#list, MAP_FLIGHTS) do
    local b = map_window["btnFlight" .. i]
    if b then b:setText(flight_label(list[i], now)) end
  end
  local acts = type(map_spec.actions) == "table" and map_spec.actions or {}
  local a = map_selected and acts[map_selected]
  if type(a) == "table" and a.opens_in and map_window.btnAct:getText() ~= "..." then
    show_button(map_window.btnAct, a, now)   -- Fly opens
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

-- The scale bar: the longest round distance that fits in 220 px at the current zoom.
local function update_scale()
  if not (map_window and map_view) then return end
  local _, ew = view_span(map_view)
  local mpp = ew / math.max(map_vw, 1)          -- metres per pixel
  if mpp <= 0 then return end
  local nm = SCALE_NICE[1]
  for _, n in ipairs(SCALE_NICE) do
    if n * NM / mpp <= 220 then nm = n end
  end
  local px = math.max(2, math.floor(nm * NM / mpp + 0.5))
  local x, y = map_window.pnlScale:getBounds()
  map_window.pnlScale:setBounds(x, y, px, 4)
  local _, ty, tw, th = map_window.txtScale:getBounds()
  map_window.txtScale:setBounds(x + px + 8, ty, tw, th)
  map_window.txtScale:setText(nm .. " nm")
  local bx, by, _, bh = map_window.pnlScaleBg:getBounds()
  map_window.pnlScaleBg:setBounds(bx, by, px + 8 + 70 + 16, bh)
end

local function check_zoom(view)
  pcall(update_scale)
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
  local act_y = info_y - 8 - 30
  local join_y = act_y - 34
  local flights_y = join_y - 8 - 24 - MAP_FLIGHTS * 32
  local children = {
    txtPanel = static(x0, HEADER + 8, pw, flights_y - HEADER - 16, "", 14, "0xd0d0d0ff"),
    -- The scale bar, on the map's bottom-left corner (Run 27): a filled strip as long as the
    -- distance in its label (update_scale), on a dark backing. Moved above the map view in build_map.
    pnlScaleBg = bar(12, H - 48, 200, 36, "0x000000a0"),
    pnlScale = bar(20, H - 32, 100, 4, "0xffffffff"),
    txtScale = static(128, H - 42, 80, 22, "", 14, "0xffffffff"),
    txtFlights = static(x0, flights_y, pw, 22, "UPCOMING FLIGHTS", 14, "0xd0d0d0ff"),
    txtInfo = static(x0, info_y, pw, info_h, "", 14, "0xffe080ff"),
    btnModeMap = button(x0, buttons_y, bw, 28, "Map"),
    btnModeAlt = button(x0 + bw + pad, buttons_y, bw, 28, "Terrain"),
    btnModeSat = button(x0 + 2 * (bw + pad), buttons_y, bw, 28, "Satellite"),
    btnCenter = button(x0, buttons_y + 34, pw, 28, "Back to the front"),
    btnJoin = button(x0, join_y, pw, 28, "Join"),
    btnAct = button(x0, act_y, pw, 28, "Commit"),
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

-- Fly: the jet, not the map (it stays for the hotkey).
close_war_map = function()
  if map_window then map_window:setVisible(false) end
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
  -- The scale bar's widgets came with the window, under the map view: put them back on top.
  local ok_s, err_s = pcall(function()
    for _, k in ipairs({ "pnlScaleBg", "pnlScale", "txtScale" }) do
      map_window:removeWidget(map_window[k])
      map_window:insertWidget(map_window[k])
    end
  end)
  map_event("map", "step scale bar " .. (ok_s and "over the map" or ("failed: " .. tostring(err_s))))
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
  for name, field in pairs({ btnJoin = "joins", btnAct = "actions" }) do
    local b = map_window[name]
    b:setVisible(false)
    b:addChangeCallback(function()
      local all = map_spec and type(map_spec[field]) == "table" and map_spec[field] or {}
      local j = map_selected and all[map_selected]
      local _, ok = action_of(j, model_time())
      if not ok or not j.key or j.key == "" then return end
      map_event("map_click", "button " .. tostring(j.key))
      to_server("campaignClick", tostring(j.key))
      b:setText("...")       -- the server answers with a fresh map
      b:setEnabled(false)
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

local auto_open = nil      -- os.clock() at which to open the map (a blue slot was just taken)
local auto_pending = false -- ...but no map had come yet: open the next one that does

local function open_map(spec)
  local t0 = os.clock()
  if auto_pending and spec.show == false then   -- the first map after taking a blue slot
    auto_pending = false
    spec.show = true
  end
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
  pcall(update_scale)
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

-- ---------- P22: ride to the jet (the camera follows a vehicle the server drives) ----------
-- campaignRide(spec) starts it, campaignRidePos(p) is the vehicle's latest position from the
-- server, campaignRideStop() ends it (so does entering a jet). The camera goes to a seat in the
-- vehicle (or behind it, view = "chase") with Export.LoSetCameraPosition, in a view that lets it
-- (see ride_frame). Where the
-- vehicle is: from Export.LoGetWorldObjects when DCS lets this PC read objects (always for the
-- host; for a client, the server's allow_object_export), else from the server's positions,
-- carried forward at the vehicle's speed and smoothed. Export coordinates: x north, y up, z east;
-- the mission's x north, y east. What happened goes back as campaignWebEvent("ride", ...).
-- spec = { unit = "P22 Humvee-1", view = "seat" | "chase", eye = { fwd, right, up } (metres),
--          native = false (don't ask for DCS's object camera: place the camera every frame),
--          look = true (placing it: keep the player's view direction; Run 29 third try: it drifted),
--          chase = { back, up } (metres), report_s = 2 }
local ride = nil

local function ride_event(detail) pcall(to_server, "campaignWebEvent", "ride", detail) end

local function export_fn(name)
  local E = Export
  if type(E) ~= "table" then return nil end
  local f = E[name]
  return type(f) == "function" and f or nil
end

local function allowed(name)
  local f = export_fn(name)
  if not f then return "n/a" end
  local ok, v = pcall(f)
  return ok and tostring(v) or ("error " .. tostring(v))
end

-- The vehicle in DCS's own world objects, if this PC may read them: its object id, or nil and why.
local function find_object(unit)
  local list_fn = export_fn("LoGetWorldObjects")
  if not list_fn then return nil, "no LoGetWorldObjects" end
  local ok, objs = pcall(list_fn, "units")
  if not ok then return nil, "LoGetWorldObjects: " .. tostring(objs) end
  if type(objs) ~= "table" then return nil, "LoGetWorldObjects gave " .. type(objs) end
  local n = 0
  for id, o in pairs(objs) do
    n = n + 1
    if type(o) == "table" and o.UnitName == unit then return id, n .. " objects" end
  end
  return nil, "not among " .. n .. " objects"
end

local function now_s() return Sim.getRealTime() end

-- Where the vehicle is now: x north, z east (Export coordinates), heading (radians, from north),
-- y of the ground (or the object's own), and which source it came from.
local function vehicle_now()
  if ride.obj then
    local get = export_fn("LoGetObjectById")
    local ok, o = pcall(get, ride.obj)
    if ok and type(o) == "table" and type(o.Position) == "table" then
      return o.Position.x, o.Position.y, o.Position.z, tonumber(o.Heading) or 0, "objects",
             tonumber(o.Pitch) or 0, tonumber(o.Bank) or 0
    end
    ride.obj = nil
    ride_event("object lost: falling back to the server's positions")
  end
  local p = ride.last
  if not p then return nil end
  local dt = math.min(now_s() - p.at, 1.0)   -- carry it forward at its speed, at most 1 s
  local h = p.hdg
  local x, z = p.x + math.cos(h) * p.spd * dt, p.z + math.sin(h) * p.spd * dt
  local s = ride.smooth
  if not s then
    s = { x = x, z = z, h = h }
    ride.smooth = s
  else
    local a = math.min(1, (now_s() - (s.at or now_s())) / 0.25)   -- about 0.25 s to catch up
    s.x, s.z = s.x + (x - s.x) * a, s.z + (z - s.z) * a
    local dh = (h - s.h + math.pi) % (2 * math.pi) - math.pi
    s.h = s.h + dh * a
  end
  s.at = now_s()
  local y = p.alt
  local alt_fn = export_fn("LoGetAltitude")
  if alt_fn then
    local ok, g = pcall(alt_fn, s.x, s.z)
    if ok and tonumber(g) then y = tonumber(g) end
  end
  return s.x, y, s.z, s.h, "server"
end

local function v3(x, y, z) return { x = x, y = y, z = z } end

-- The camera for vehicle position (x, y, z) and heading h: orientation x forward, y up, z right.
-- Turn a vector by a (radians, clockwise seen from above) about the vertical.
local function yaw(v, a)
  local c, s = math.cos(a), math.sin(a)
  return v3(v.x * c - v.z * s, v.y, v.x * s + v.z * c)
end

-- Looking around (version 19, Run 29: the view was fixed). While the camera holds, the orientation
-- DCS read back last frame, with the player's mouse or head movement in it, is kept, turned with the
-- vehicle; only the position is the script's.
local function looked(cam, h)
  local l = ride.look_on and ride.look
  if l and ride.look_raw then   -- mounted on the Humvee: DCS's direction is already the Humvee's
    cam.x, cam.y, cam.z = l.x, l.y, l.z
  elseif l then
    local a = h - l.h
    cam.x, cam.y, cam.z = yaw(l.x, a), yaw(l.y, a), yaw(l.z, a)
  end
  ride.cam_h = h
  return cam
end

local function camera_for(x, y, z, h)
  local fx, fz = math.cos(h), math.sin(h)    -- forward, flat
  local rx, rz = -fz, fx                     -- right
  if ride.view == "chase" then
    local back, up = ride.chase[1], ride.chase[2]
    local px, py, pz = x - fx * back, y + up, z - fz * back
    local tx, ty, tz = x + fx * 5, y + 1.5, z + fz * 5   -- look a little ahead of it
    local dx, dy, dz = tx - px, ty - py, tz - pz
    local n = math.sqrt(dx * dx + dy * dy + dz * dz)
    dx, dy, dz = dx / n, dy / n, dz / n
    local hn = math.sqrt(dx * dx + dz * dz)
    -- up = right x forward, with right flat
    local ux, uy, uz = -dy * dx / hn, hn, -dy * dz / hn
    return looked({ x = v3(dx, dy, dz), y = v3(ux, uy, uz), z = v3(-dz / hn, 0, dx / hn), p = v3(px, py, pz) }, h)
  end
  local e = ride.eye
  return looked({ x = v3(fx, 0, fz), y = v3(0, 1, 0), z = v3(rx, 0, rz),
                  p = v3(x + fx * e[1] + rx * e[2], y + e[3], z + fz * e[1] + rz * e[2]) }, h)
end

stop_ride = function(why)
  if not ride then return end
  local r = ride
  ride = nil
  ride_event(string.format("stopped (%s) after %.0f s, %s: %d frames, %d tries, camera held %d, moved by DCS %d, set failed %d",
    why, now_s() - r.started, r.state, r.frames, r.tries, r.held, r.moved, r.failed))
  log_info("ride stopped: " .. why)
end

local ride_frame   -- placing the camera ourselves (below)

local function look_for_object()
  if ride.obj or now_s() < ride.next_find then return end   -- object export may come and go; look now and then
  ride.next_find = now_s() + (ride.want_native and 0.5 or 5)
  local id, why = find_object(ride.unit)
  ride.obj = id
  if id ~= ride.found then
    ride.found = id
    ride_event(id and ("vehicle found in world objects (id " .. tostring(id) .. ", " .. why .. ")")
                  or ("vehicle not readable here: " .. why))
  end
end

-- ---- Version 20: DCS's own camera on the vehicle (LoCreateCameraRequest / LoForceCamera) ----
-- Run 29: placing the camera every frame works in F4 but fights DCS (no looking around, drift when
-- the script tried). Export.lua documents a camera request: "CameraObject" (RCtrl+F2, object
-- local camera) on an object id, optionally at a position. If this DCS has it, DCS itself follows
-- the vehicle and the player looks around as usual: first from outside (as F4 would), then inside
-- at the seat. Export.lua marks it "not enabled yet for general public release", so every step is
-- checked and reported, and without it the ride falls back to placing the camera (and tries to
-- switch to F4 itself).
local OUTSIDE_S = 3.0    -- outside the vehicle this long before moving inside
local SETTLE_S = 0.5     -- after a request, read the camera back this much later
local INSIDE_M = 3.0     -- the camera this close to the seat (each axis) is inside

local function force_camera(name, id, pos)
  local create, force = export_fn("LoCreateCameraRequest"), export_fn("LoForceCamera")
  if not force then return false, "LoForceCamera n/a" end
  create = create or function() return nil end
  local ok, req = pcall(create)
  -- Run 29 fourth try: LoCreateCameraRequest returned nil in DCS 2.9.32; build the request ourselves
  -- (the fields Export.lua documents) and see whether LoForceCamera takes it.
  if not ok or type(req) ~= "table" then
    ride.request_note = "LoCreateCameraRequest gave " .. tostring(req) .. ", request built here"
    req = { name = "CameraFree", id = 0, fov = -1.0, valid_pos = false,
            pos = { x = v3(1, 0, 0), y = v3(0, 1, 0), z = v3(0, 0, 1), p = v3(0, 0, 0) } }
  end
  req.name, req.id, req.fov = name, id, -1.0
  if pos then req.pos, req.valid_pos = pos, true else req.valid_pos = false end
  local ok2, err = pcall(force, req)
  if not ok2 then return false, "LoForceCamera: " .. tostring(err) end
  return true
end

-- The camera relative to the vehicle: forward, up, right (metres), and the distance.
local function cam_from_vehicle()
  local get, obj = export_fn("LoGetCameraPosition"), export_fn("LoGetObjectById")
  if not (get and obj and ride.obj) then return nil end
  local okc, c = pcall(get)
  local okv, o = pcall(obj, ride.obj)
  if not (okc and type(c) == "table" and type(c.p) == "table" and okv and type(o) == "table"
          and type(o.Position) == "table") then return nil end
  local h = tonumber(o.Heading) or 0
  local dx, dy, dz = c.p.x - o.Position.x, c.p.y - o.Position.y, c.p.z - o.Position.z
  local fx, fz = math.cos(h), math.sin(h)
  return dx * fx + dz * fz, dy, -dx * fz + dz * fx, math.sqrt(dx * dx + dy * dy + dz * dz)
end

local function rel_text()
  local f, u, r, d = cam_from_vehicle()
  if not f then return "camera unreadable" end
  return string.format("camera %.1f m ahead, %.1f m up, %.1f m right of it (%.0f m)", f, u, r, d)
end

local function at_seat()
  local f, u, r = cam_from_vehicle()
  local e = ride.eye
  return f and math.abs(f - e[1]) < INSIDE_M and math.abs(u - e[3]) < INSIDE_M and math.abs(r - e[2]) < INSIDE_M
end

-- Switch to F4 (camera mounted on object) as if the key were pressed: the command's number from
-- DCS's input tables. Used when the camera request isn't there.
-- Press a view key as if on the keyboard: the command's number from DCS's input tables.
local function press(command, label)
  local set = export_fn("LoSetCommand")
  if not set then return "no LoSetCommand" end
  local ok, Input = pcall(require, "Input")
  if not ok or type(Input) ~= "table" or type(Input.getEnvTable) ~= "function" then return "no Input module" end
  local ok2, env = pcall(Input.getEnvTable)
  local id = ok2 and type(env) == "table" and type(env.Actions) == "table" and env.Actions[command]
  if not id then return "no " .. command end
  local ok3, err = pcall(set, id)
  return ok3 and ("pressed " .. label .. " (command " .. tostring(id) .. ")") or ("LoSetCommand: " .. tostring(err))
end

local function press_f4() return press("iCommandViewRear", "F4") end

-- ---- Version 22: make the Humvee the object DCS is watching (Run 29, fourth try) ----
-- F4 is the camera mounted on the object being watched, and the script could move it but not
-- change the object (the bottom-left label kept the aircraft; looking around tumbled with it).
-- F7 (ground unit view) watches a ground unit, so: press F7 until the camera is near the Humvee
-- (each press cycles to the next one), then F4 to mount the camera on it. Then put the camera at
-- the seat once and watch: if DCS keeps it there as the Humvee drives, DCS follows it and the
-- player looks around as usual ("mounted at the seat"); if not, the script places it every frame,
-- keeping DCS's view direction, which now belongs to the Humvee ("placing, mounted").
local FOCUS_PRESSES = 8    -- F7 this many times at most
local FOCUS_M = 80         -- the camera this close to the Humvee after F7: it's watching it
local WATCH_S = 2.0        -- after putting the camera at the seat once, watch this long

local function focus_start(why)
  ride.focus = { stage = "f7", at = now_s(), presses = 1 }
  ride_event("focus: " .. why .. "; " .. press("iCommandViewGround", "F7"))
end

local function focus_fail(why)
  ride.focus = nil
  ride_event("focus: " .. why .. "; placing the camera instead; " .. press_f4())
end

-- The seat camera with the vehicle's whole attitude, not just its heading (Run 29, fifth try: placed
-- once on a slope, the view stayed tilted, since DCS then keeps it fixed to the vehicle). Axes: x
-- forward, y up, z right; pitch nose up, bank right side down (both radians, as LoGetObjectById).
local function seat_cam()
  local x, y, z, h, _, p, b = vehicle_now()
  if not x then return nil end
  p, b = p or 0, b or 0
  local ch, sh, cp, sp, cb, sb = math.cos(h), math.sin(h), math.cos(p), math.sin(p), math.cos(b), math.sin(b)
  local f = v3(cp * ch, sp, cp * sh)
  local r0, u0 = v3(-sh, 0, ch), v3(-sp * ch, cp, -sp * sh)
  local r = v3(r0.x * cb - u0.x * sb, r0.y * cb - u0.y * sb, r0.z * cb - u0.z * sb)
  local u = v3(u0.x * cb + r0.x * sb, u0.y * cb + r0.y * sb, u0.z * cb + r0.z * sb)
  local e = ride.eye
  ride.seat_attitude = string.format("heading %.1f, pitch %.1f, bank %.1f deg", math.deg(h) % 360, math.deg(p), math.deg(b))
  return { x = f, y = u, z = r, p = v3(x + f.x * e[1] + u.x * e[3] + r.x * e[2], y + f.y * e[1] + u.y * e[3] + r.y * e[2],
                                       z + f.z * e[1] + u.z * e[3] + r.z * e[2]) }
end

local function focus_frame()
  local f, t = ride.focus, now_s()
  if t < f.at + SETTLE_S then return end
  if f.stage == "f7" then
    local _, _, _, d = cam_from_vehicle()
    if d and d <= FOCUS_M then
      f.stage, f.at = "f4", t
      ride_event(string.format("focus: watching the Humvee after %d F7 (%.0f m); %s", f.presses, d, press_f4()))
    elseif f.presses >= FOCUS_PRESSES then
      focus_fail(string.format("F7 didn't reach the Humvee in %d presses (%s)", f.presses, rel_text()))
    else
      f.presses, f.at = f.presses + 1, t
      ride_event("focus: F7 landed " .. rel_text() .. "; " .. press("iCommandViewGround", "F7"))
    end
  elseif f.stage == "f4" then
    ride_event("focus: F4 on the Humvee: " .. rel_text())
    if ride.view == "chase" then   -- DCS's own camera on the Humvee: leave it to DCS
      f.stage, f.next_report = "dcs", t + ride.report_s
      ride_event("focus: chase view: DCS's F4 camera on the Humvee, look around as usual")
      return
    end
    local set = export_fn("LoSetCameraPosition")
    local cam = seat_cam()
    if not (set and cam and pcall(set, cam)) then return focus_fail("couldn't place the camera at the seat") end
    f.stage, f.at, f.placed, f.next_check = "watch", t - SETTLE_S, t, t   -- checks at 0.5, 1, 1.5, 2 s
    ride_event("focus: placed at the seat, the Humvee at " .. tostring(ride.seat_attitude))
  elseif f.stage == "watch" then
    if t < f.next_check + SETTLE_S then return end
    f.next_check = t
    ride_event(string.format("focus: %.1f s after placing it once: %s", t - f.placed, rel_text()))
    if not at_seat() then
      f.stage = "placing"
      ride.look_on, ride.look_raw = ride.want_look, true
      ride.state, ride.next_try = "waiting", 0
      ride_event("focus: DCS doesn't keep it at the seat; placing it every frame, mounted on the Humvee"
                 .. (ride.look_on and ", your view direction kept" or ", view fixed ahead"))
    elseif now_s() >= f.placed + WATCH_S then
      f.stage, f.next_report = "dcs", now_s() + ride.report_s
      ride_event("focus: mounted at the seat: DCS follows the Humvee, look around as usual")
    end
  elseif f.stage == "dcs" and t >= f.next_report then
    f.next_report = t + ride.report_s
    local _, _, _, d = cam_from_vehicle()
    if d and d > 200 and not f.away then
      f.away = true
      ride_event("focus: the view is elsewhere now (" .. rel_text() .. ")")
    end
    ride_event("focus, DCS's camera: " .. rel_text())
  end
end

local function native_start()
  local ok, err = force_camera("CameraObject", ride.obj, nil)
  if not ok then
    ride.want_native = false
    focus_start("native: " .. err)
    return
  end
  ride.native = { stage = "outside", at = now_s(), next_report = now_s() + SETTLE_S }
  ride_event("native: asked for DCS's object camera on the vehicle" .. (ride.request_note and (" (" .. ride.request_note .. ")") or ""))
end

local function native_frame()
  local n, t = ride.native, now_s()
  if n.stage == "outside" then
    if t >= n.next_report then
      n.next_report = math.huge
      local f, u, r, d = cam_from_vehicle()
      ride_event("native, outside: " .. rel_text())
      if d and d <= 200 then ride_event("native: the view came to the vehicle") end
      if not d or d > 200 then   -- LoForceCamera took the request but the view didn't come
        ride.native, ride.want_native = nil, false
        focus_start("native: the view didn't go to the vehicle")
        return
      end
    end
    if t >= n.at + OUTSIDE_S and ride.view == "chase" then   -- chase: DCS's camera stays outside
      n.stage, n.next_report = "outside only", t + ride.report_s
      ride_event("native: staying outside (chase view)")
    elseif t >= n.at + OUTSIDE_S then
      local e = ride.eye   -- the object's own axes: x forward, y up, z right
      local ok, err = force_camera("CameraObject", ride.obj, { x = v3(1, 0, 0), y = v3(0, 1, 0), z = v3(0, 0, 1),
                                                               p = v3(e[1], e[3], e[2]) })
      n.stage, n.at, n.how = "inside, local", t, "vehicle axes"
      ride_event(ok and "native: asked for the seat (position in the vehicle's axes)" or ("native: seat failed: " .. err))
    end
  elseif n.stage == "inside, local" or n.stage == "inside, world" then
    if t < n.at + SETTLE_S then return end
    if at_seat() then
      n.stage, n.next_report, n.seated = "inside", t + ride.report_s, t
      ride_event("native: in the seat (" .. n.how .. "): " .. rel_text())
    elseif n.stage == "inside, local" then
      local x, y, z, h = vehicle_now()
      ride_event("native: not at the seat with the vehicle's axes: " .. rel_text() .. "; trying the world position")
      if x then
        local look = ride.look_on
        ride.look_on = false
        local cam = camera_for(x, y, z, h)
        ride.look_on = look
        force_camera("CameraObject", ride.obj, cam)
      end
      n.stage, n.at, n.how = "inside, world", t, "world position"
    else
      n.stage = "outside only"
      ride_event("native: not at the seat with the world position either: " .. rel_text()
                 .. "; staying outside (DCS's camera, look around as usual)")
      force_camera("CameraObject", ride.obj, nil)
      n.next_report = t + ride.report_s
    end
  elseif t >= n.next_report then
    n.next_report = t + ride.report_s
    local f, u, r, d = cam_from_vehicle()
    if n.stage == "inside" and f and not at_seat() and not n.left then
      n.left = true   -- moved off the seat: the vehicle drove on without it, or the player moved it
      ride_event("native: the camera left the seat: " .. rel_text())
    end
    if d and d > 200 and not n.away then
      n.away = true
      ride_event("native: the view is elsewhere now (" .. rel_text() .. ")")
    end
    ride_event("native, " .. n.stage .. ": " .. rel_text())
  end
end

-- Every frame while riding: DCS's own camera if it has one for us, else the script places it.
local function ride_tick()
  if not ride then return end
  if ride.want_native and not ride.native then
    look_for_object()
    if ride.obj then
      native_start()
    elseif now_s() - ride.started > 3 then
      ride.want_native = false
      ride_event("native: the vehicle isn't in this PC's world objects; placing the camera instead; " .. press_f4())
    end
    if ride.want_native then return end
  end
  if ride.native then
    native_frame()
    return
  end
  if ride.focus and ride.focus.stage ~= "placing" then
    focus_frame()
    return
  end
  ride_frame()
end

-- Run 29: in the view that follows a spectated aircraft DCS puts the camera back every frame; in
-- F4 the camera stayed where it was put; and DCS crashed (access violation, from Lua) on F5 mid-ride.
-- So the script only tries to place the camera once a second until it holds ("waiting"), then
-- every frame ("holding"); the moment DCS moves it (the view changed) it lets go and makes no more
-- camera calls this ride ("let go": -ride again to retry).
local TRY_S = 1.0      -- while waiting, one try this often (ride_frame is declared above)
local HOLD_M = 2.0     -- the camera read back this close to where it was put: it held
local LET_GO_M = 25.0  -- holding, DCS moved it this far: the view changed (Run 29: it nudges it 3 to 4 m)
ride_frame = function()
  if not ride or ride.state == "let go" then return end
  local set_fn, get_fn = export_fn("LoSetCameraPosition"), export_fn("LoGetCameraPosition")
  if not (set_fn and get_fn) then
    ride.state = "let go"
    ride_event("let go: no LoSetCameraPosition/LoGetCameraPosition here")
    return
  end
  -- Did the camera stay where we put it last frame?
  if ride.put then
    local ok, c = pcall(get_fn)
    local d = math.huge
    if ok and type(c) == "table" and type(c.p) == "table" then
      d = math.sqrt((c.p.x - ride.put.x) ^ 2 + (c.p.y - ride.put.y) ^ 2 + (c.p.z - ride.put.z) ^ 2)
    end
    ride.put = nil
    if d < HOLD_M then
      ride.held = ride.held + 1
      if type(c.x) == "table" and type(c.y) == "table" and type(c.z) == "table" then
        ride.look = { x = c.x, y = c.y, z = c.z, h = ride.cam_h or 0 }
      end
      if ride.state == "waiting" then
        ride.state = "holding"
        ride_event(string.format("holding: the camera is in the Humvee (after %d tries, %.0f s)", ride.tries,
          now_s() - ride.started))
      end
    else
      ride.moved = ride.moved + 1
      ride.last_off = d
      if ride.state == "holding" and d >= LET_GO_M then
        ride.state = "let go"
        ride_event(string.format("let go: the view changed (camera %.0f m from where it was put) after %d frames held",
          d, ride.held))
        return
      end
    end
  end
  look_for_object()
  local x, y, z, h, src = vehicle_now()
  if not x then return end
  ride.frames = ride.frames + 1
  if src ~= ride.src then ride.src = src; ride_event("following from " .. src) end
  if ride.state == "holding" or now_s() >= ride.next_try then
    if ride.state == "waiting" then ride.next_try, ride.tries = now_s() + TRY_S, ride.tries + 1 end
    local cam = camera_for(x, y, z, h)
    local ok, err = pcall(set_fn, cam)
    if ok then ride.put = cam.p else
      ride.failed = ride.failed + 1
      if not ride.err then ride.err = tostring(err); ride_event("LoSetCameraPosition failed: " .. ride.err) end
    end
  end
  if now_s() >= ride.next_report then
    ride.next_report = now_s() + ride.report_s
    ride_event(string.format("%s, %s view, from %s: %d frames, %d tries, held %d, moved by DCS %d%s, set failed %d; at x %.0f z %.0f hdg %.0f",
      ride.state, ride.view, src, ride.frames, ride.tries, ride.held, ride.moved,
      ride.last_off and string.format(" (last %.0f m off)", ride.last_off) or "", ride.failed, x, z, math.deg(h) % 360))
  end
end

function RPC.method.campaignRide(sender_id, spec)
  if not from_server(sender_id) then return end
  spec = type(spec) == "table" and spec or {}
  local function nums(t, d)
    local out = {}
    for i = 1, #d do out[i] = tonumber(type(t) == "table" and t[i]) or d[i] end
    return out
  end
  ride = { unit = tostring(spec.unit or ""), view = spec.view == "chase" and "chase" or "seat",
           eye = nums(spec.eye, { 0.2, 0.45, 1.55 }), look_on = spec.look == true and spec.native == false,
           want_look = spec.look ~= false, want_native = spec.native ~= false, chase = nums(spec.chase, { 12, 5 }),
           report_s = tonumber(spec.report_s) or 2, started = now_s(), next_find = 0, next_report = now_s() + 1,
           found = false,   -- not looked yet: the first look is always reported
           state = "waiting", next_try = 0, tries = 0,
           frames = 0, held = 0, moved = 0, failed = 0 }
  auto_open, auto_pending = nil, false
  pcall(close_map)
  local cam = "n/a"
  local get_fn = export_fn("LoGetCameraPosition")
  if get_fn then
    local ok, c = pcall(get_fn)
    cam = ok and type(c) == "table" and type(c.p) == "table"
          and string.format("x %.0f y %.0f z %.0f", c.p.x, c.p.y, c.p.z) or ("unreadable: " .. tostring(c))
  end
  ride_event(string.format("started, %s view; Export %s; object export %s, ownship export %s; camera request %s; camera was %s",
    ride.view, type(Export), allowed("LoIsObjectExportAllowed"), allowed("LoIsOwnshipExportAllowed"),
    (export_fn("LoForceCamera") and export_fn("LoCreateCameraRequest")) and "available" or "n/a", cam))
  log_info("ride started: " .. ride.unit)
end

-- p = { x, y (the mission's: north, east), alt, hdg (degrees, true), spd (m/s) }
function RPC.method.campaignRidePos(sender_id, p)
  if not from_server(sender_id) or not ride or type(p) ~= "table" then return end
  local x, z = tonumber(p.x), tonumber(p.y)
  if not (x and z) then return end
  ride.last = { x = x, z = z, alt = tonumber(p.alt) or 0, hdg = math.rad(tonumber(p.hdg) or 0),
                spd = tonumber(p.spd) or 0, at = now_s() }
end

-- Move the seat camera during the ride (version 24: tuning it to the passenger figure's head):
-- eye = { fwd, right, up } (metres). With DCS's camera on the vehicle, the camera is placed once more
-- at the new seat (DCS keeps it there); while the script places it, it uses the new seat next frame.
function RPC.method.campaignRideEye(sender_id, eye)
  if not from_server(sender_id) or not ride or type(eye) ~= "table" then return end
  for i = 1, 3 do ride.eye[i] = tonumber(eye[i]) or ride.eye[i] end
  local f = ride.focus
  if f and f.stage == "dcs" and ride.view == "seat" then
    local set, cam = export_fn("LoSetCameraPosition"), seat_cam()
    if set and cam then pcall(set, cam) end
  end
  ride_event(string.format("eye now %.2f forward, %.2f right, %.2f up", ride.eye[1], ride.eye[2], ride.eye[3]))
end

function RPC.method.campaignRideStop(sender_id, why)
  if not from_server(sender_id) then return end
  stop_ride(tostring(why or "server"))
end

local callbacks = {}

function callbacks.onPlayerChangeSlot(id)
  if pending_join and id == net.get_my_player_id() then
    pending_join = false
    enter()
  end
end

-- Opening the map by itself (AUTO_OPEN_MAP): when this player's side or slot changes to blue, the
-- map opens once the slot screen has gone. Run 27: the host choosing blue got no
-- onPlayerChangeSlot, only a coalition change, so both are watched here. The host's game is
-- paused while its slot screen is open, so unpaused means it's gone. Only for a blue spectator,
-- never in a jet (Run 28: after Fly, the slot change opened it over the cockpit); getting into
-- an aircraft also cancels an opening that's on its way.
local slot_key, slot_armed, slot_check = nil, false, 0

local function watch_slot()
  if os.clock() - slot_check < 0.5 then return end
  slot_check = os.clock()
  local me = net.get_my_player_id()
  local side = tonumber(net.get_player_info(me, "side")) or 0
  local slot = tostring(net.get_player_info(me, "slot") or "")
  local key = side .. ":" .. slot
  if key ~= slot_key then
    log_info("map: now side " .. key)
    slot_key = key
    local in_jet = slot ~= "" and slot ~= "0"
    slot_armed = side == 2 and not in_jet
    if in_jet then auto_open, auto_pending = nil, false end
  end
  if slot_armed then
    local paused = false
    pcall(function() paused = Sim.getPause() end)
    if not paused then
      slot_armed = false
      auto_open = os.clock() + 1.0
      log_info("map: opens in a moment (blue spectator)")
    end
  end
end

local function auto_open_map()
  if not auto_open or os.clock() < auto_open then return end
  auto_open = nil
  if ride then return end   -- riding to the jet (P22): the camera is the show
  if map_window and map_window:getVisible() then return end
  if map_spec then
    toggle_map()
  else
    auto_pending = true   -- the server's first map isn't here yet
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
  if ride then
    local ok, err = pcall(ride_tick)
    if not ok then
      ride_event("frame error: " .. tostring(err))
      ride = nil
    end
  end
  if AUTO_OPEN_MAP then
    local ok, err = pcall(watch_slot)
    if not ok and slot_check >= 0 then
      log_error("map slot watch: " .. tostring(err))
      slot_check = math.huge   -- once
    end
  end
  if auto_open then
    local ok, err = pcall(auto_open_map)
    if not ok then log_error("map auto open: " .. tostring(err)) end
  end
end

function callbacks.onSimulationStop()
  ride = nil
  auto_open, auto_pending = nil, false
  slot_key, slot_armed, slot_check = nil, false, 0
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
