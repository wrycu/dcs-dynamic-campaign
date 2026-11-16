-- Tests the shared Lua transport against an in-memory socket. Run from the repo root:
--   luajit tests/lua/test_transport.lua
-- (Lua 5.1 semantics, same as DCS.)

local function read_file(path)
  local f = assert(io.open(path, "rb"))
  local s = f:read("*a")
  f:close()
  return s
end

local src = read_file("lua/mission/campaign_bridge.lua")
local block = assert(src:match("(%-%- BEGIN TRANSPORT\n.-%-%- END TRANSPORT\n)"), "transport block not found")
local new_transport = assert(loadstring(block .. "\nreturn new_transport", "transport"))()

-- ---------- codec: Lua literals on one line instead of JSON (net.* is DCS-only) ----------

local function serialize(v)
  local t = type(v)
  if t == "string" then return string.format("%q", v):gsub("\\\n", "\\n") end
  if t == "number" or t == "boolean" or t == "nil" then return tostring(v) end
  local parts = {}
  for k, val in pairs(v) do
    local key = type(k) == "string" and string.format("[%q]", k) or "[" .. tostring(k) .. "]"
    parts[#parts + 1] = key .. "=" .. serialize(val)
  end
  return "{" .. table.concat(parts, ",") .. "}"
end
local function deserialize(s) return assert(loadstring("return " .. s))() end

-- ---------- in-memory socket ----------

local server = { refuse = false, closed = false, inbox = "", outbox = "", send_limit = nil, connects = 0 }

local socket_lib = {}
function socket_lib.tcp()
  local s = {}
  function s:settimeout() end
  function s:setoption() end
  function s:close() end
  function s:getpeername() return "127.0.0.1", 7341 end
  function s:connect()
    if server.refuse then return nil, "connection refused" end
    server.connects = server.connects + 1
    server.closed, server.inbox, server.outbox = false, "", ""
    return 1
  end
  function s:send(data, i)
    if server.closed then return nil, "closed", i - 1 end
    local j = #data
    if server.send_limit then j = math.min(#data, i + server.send_limit - 1) end
    server.inbox = server.inbox .. data:sub(i, j)
    if j < #data then return nil, "timeout", j end
    return j
  end
  function s:receive(_, prefix)
    prefix = prefix or ""
    if server.closed then return nil, "closed", prefix end
    local nl = server.outbox:find("\n", 1, true)
    if nl then
      local line = prefix .. server.outbox:sub(1, nl - 1)
      server.outbox = server.outbox:sub(nl + 1)
      return line
    end
    local part = prefix .. server.outbox
    server.outbox = ""
    return nil, "timeout", part
  end
  return s
end
function socket_lib.select() return {}, {} end

-- ---------- harness ----------

local clock = 0
local received = {}
local T = new_transport({
  host = "127.0.0.1", port = 7341, src = "mission", socket = socket_lib,
  encode = serialize, decode = deserialize, now = function() return clock end, log = function() end,
  max_queue = 50, retain = 100, reconnect_interval = 5, connect_timeout = 5,
  max_lines = 100, max_bytes = 1024 * 1024,
  hello_data = function() return { run_id = "r1" } end,
  on_message = function(msg) received[#received + 1] = msg end,
})

local function drain_inbox()
  local msgs = {}
  for line in server.inbox:gmatch("([^\n]*)\n") do msgs[#msgs + 1] = deserialize(line) end
  server.inbox = server.inbox:match("[^\n]*$")
  return msgs
end

local function seqs(msgs)
  local out = {}
  for _, m in ipairs(msgs) do out[#out + 1] = m.seq end
  return table.concat(out, ",")
end

local function welcome(last_seq)
  server.outbox = server.outbox .. serialize({ v = 1, type = "welcome", src = "engine", seq = 0, data = { last_seq = last_seq } }) .. "\n"
end

local function eq(a, b, what)
  if a ~= b then error(string.format("%s: expected %s, got %s", what, tostring(b), tostring(a)), 2) end
end

local tests = {}

function tests.handshake_then_flush_in_order()
  T.send("event", { n = 1 }, 0)
  T.send("event", { n = 2 }, 0)
  T.send_volatile("telemetry", {}, 0)  -- dropped: not connected
  T.tick()
  eq(T.state, "handshake", "state after connect")
  local msgs = drain_inbox()
  eq(#msgs, 1, "only hello before welcome")
  eq(msgs[1].type, "hello", "first message")
  eq(msgs[1].data.run_id, "r1", "hello run_id")

  welcome(0)
  T.tick()
  eq(T.state, "ready", "state after welcome")
  T.send("event", { n = 3 }, 0)
  T.tick()
  eq(seqs(drain_inbox()), "1,2,3", "flushed seqs")
end

function tests.partial_send_keeps_lines_intact()
  server.send_limit = 7
  T.send("event", { text = string.rep("x", 100) }, 0)
  T.send("event", { n = 5 }, 0)
  for _ = 1, 100 do T.tick() end
  server.send_limit = nil
  local msgs = drain_inbox()
  eq(seqs(msgs), "4,5", "seqs after partial sends")
  eq(msgs[1].data.text, string.rep("x", 100), "payload intact")
end

function tests.partial_receive_reassembles_lines()
  local line = serialize({ v = 1, type = "cmd", src = "engine", seq = 1, id = "c1", data = { op = "query" } })
  local half = math.floor(#line / 2)
  server.outbox = line:sub(1, half)
  T.tick()
  eq(#received, 0, "no message from half a line")
  server.outbox = line:sub(half + 1) .. "\n"
  T.tick()
  eq(#received, 1, "message after full line")
  eq(received[1].id, "c1", "cmd id")

  T.ack("c1", true, { ok = 1 })
  T.tick()
  local msgs = drain_inbox()
  eq(msgs[1].type, "ack", "ack sent")
  eq(msgs[1].seq, 0, "ack unsequenced")
  eq(msgs[1].re, "c1", "ack re")
end

function tests.reconnect_replays_unseen_messages()
  -- seqs 1..5 have been sent. The server crashes having processed only up to 3.
  server.closed = true
  T.send("event", { n = 6 }, 0)
  T.tick()
  eq(T.state, "disconnected", "state after drop")
  T.send("event", { n = 7 }, 0)
  T.ack("c2", true)  -- volatile: dropped while disconnected

  T.tick()
  eq(T.state, "disconnected", "waits for reconnect interval")
  clock = clock + 5
  T.tick()
  eq(T.state, "handshake", "reconnected")
  eq(drain_inbox()[1].type, "hello", "hello on reconnect")

  welcome(3)
  T.tick()
  T.tick()
  eq(seqs(drain_inbox()), "4,5,6,7", "replayed seqs")
end

function tests.refused_connect_backs_off()
  server.closed = true
  T.tick()
  server.refuse = true
  local before = server.connects
  clock = clock + 5
  T.tick()
  eq(T.state, "disconnected", "refused")
  T.tick()
  clock = clock + 5
  server.refuse = false
  T.tick()
  eq(server.connects, before + 1, "one successful connect after backoff")
  drain_inbox()
  welcome(7)
  T.tick()
  eq(T.state, "ready", "ready again")
end

function tests.queue_cap_drops_oldest()
  server.closed = true
  T.tick()
  for i = 1, 60 do T.send("event", { i = i }, 0) end  -- max_queue = 50
  clock = clock + 5
  T.tick()
  drain_inbox()
  welcome(7)
  T.tick()
  local msgs = drain_inbox()
  eq(#msgs, 50, "queue capped")
  eq(msgs[1].data.i, 11, "oldest dropped")
  eq(msgs[50].data.i, 60, "newest kept")
end

local order = {
  "handshake_then_flush_in_order", "partial_send_keeps_lines_intact",
  "partial_receive_reassembles_lines", "reconnect_replays_unseen_messages",
  "refused_connect_backs_off", "queue_cap_drops_oldest",
}
local failed = 0
for _, name in ipairs(order) do
  local ok, err = pcall(tests[name])
  print((ok and "ok   " or "FAIL ") .. name .. (ok and "" or (": " .. tostring(err))))
  if not ok then failed = failed + 1 end
end
os.exit(failed == 0 and 0 or 1)
