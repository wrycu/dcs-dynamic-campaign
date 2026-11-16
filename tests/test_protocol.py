import json
import unittest

from campaign.protocol import (
    DcsName, Envelope, Hello, MsgType, ProtocolError, Src, as_list, decode, encode,
    group_name, parse_dcs_name, unit_name,
)


class EnvelopeTests(unittest.TestCase):
    def test_round_trip(self):
        env = Envelope(type=MsgType.CMD, src=Src.ENGINE, seq=3, id="c1",
                       data={"op": "despawn_group", "group": "gu:a1"})
        line = encode(env)
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)
        self.assertEqual(decode(line), env)

    def test_omits_unset_optional_fields(self):
        raw = json.loads(encode(Envelope(type=MsgType.EVENT, src=Src.MISSION, seq=1)))
        self.assertEqual(set(raw), {"v", "type", "src", "seq", "data"})

    def test_empty_lua_table_data_is_accepted(self):
        env = decode(b'{"v":1,"type":"ack","src":"mission","seq":0,"re":"c1","ok":true,"data":[]}')
        self.assertEqual(env.data, {})
        self.assertTrue(env.ok)

    def test_rejects_bad_messages(self):
        for line in [
            b"not json",
            b"[1,2]",
            b'{"v":2,"type":"event","src":"mission","data":{}}',
            b'{"v":1,"type":"bogus","src":"mission","data":{}}',
            b'{"v":1,"type":"event","data":{}}',
            b'{"v":1,"type":"event","src":"mission","seq":-1,"data":{}}',
            b'{"v":1,"type":"event","src":"mission","data":"x"}',
        ]:
            with self.subTest(line=line), self.assertRaises(ProtocolError):
                decode(line)

    def test_hello_requires_run_id(self):
        env = decode(b'{"v":1,"type":"hello","src":"mission","data":{"map":"Caucasus"}}')
        with self.assertRaises(ProtocolError):
            Hello.from_envelope(env)
        env.data["run_id"] = "r1"
        self.assertEqual(Hello.from_envelope(env).spawned, [])


class AsListTests(unittest.TestCase):
    def test_lua_shapes(self):
        self.assertEqual(as_list(None), [])
        self.assertEqual(as_list({}), [])  # empty Lua table, as seen in DCS 2.9.31 telemetry
        self.assertEqual(as_list([1, 2]), [1, 2])
        self.assertEqual(as_list({"3": "c", "1": "a"}), ["a", "c"])  # sparse array
        with self.assertRaises(ProtocolError):
            as_list({"x": 1})

    def test_hello_spawned_empty_object(self):
        env = decode(b'{"v":1,"type":"hello","src":"mission","data":{"run_id":"r","spawned":{}}}')
        self.assertEqual(Hello.from_envelope(env).spawned, [])


class NamingTests(unittest.TestCase):
    def test_build_and_parse(self):
        self.assertEqual(group_name("gu", "2f9a"), "gu:2f9a")
        self.assertEqual(unit_name("fl", "51c0", 2), "fl:51c0#2")
        self.assertEqual(parse_dcs_name("gu:2f9a"), DcsName("gu", "2f9a"))
        self.assertEqual(parse_dcs_name("fl:51c0#2"), DcsName("fl", "51c0", 2))

    def test_foreign_names(self):
        for name in [None, "", "Aerial-1", "Aerial-1-1", "xx:abc", "gu:", "gu:abc#", "gu:abc#x", "gu:#1"]:
            with self.subTest(name=name):
                self.assertIsNone(parse_dcs_name(name))

    def test_invalid_ids_rejected(self):
        for bad in ["", "a:b", "a#1"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                group_name("gu", bad)
        with self.assertRaises(ValueError):
            group_name("zz", "abc")


if __name__ == "__main__":
    unittest.main()
