import json
import unittest
from pathlib import Path

from app.traffic import schema, wire

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


class WireTests(unittest.TestCase):
    def test_roundtrip_and_padding(self):
        data = wire.encode(wire.TYPE_PROBE, 7, 123456789012, 111, 222, size=200)
        self.assertEqual(len(data), 200)
        p = wire.decode(data)
        self.assertEqual((p.ptype, p.session_id, p.seq, p.send_mono_ns, p.send_wall_ns, p.size),
                         (wire.TYPE_PROBE, 7, 123456789012, 111, 222, 200))

    def test_echo_flips_type_only(self):
        data = wire.encode(wire.TYPE_PROBE, 1, 5, 10, 20, 64)
        echo = wire.make_echo(data)
        self.assertEqual(wire.decode(echo).ptype, wire.TYPE_ECHO)
        self.assertEqual(echo[6:], data[6:])
        self.assertEqual(wire.decode(echo).seq, 5)

    def test_rejects_garbage(self):
        good = wire.encode(wire.TYPE_PROBE, 1, 1, 1, 1, 64)
        for bad in (b"", b"x" * 10, b"XXXX" + good[4:], good[:4] + bytes([9]) + good[5:],
                    good[:5] + bytes([7]) + good[6:], good[:20]):
            with self.subTest(bad=bad[:8]), self.assertRaises(wire.WireError):
                wire.decode(bad)
        for size in (10, 2000):
            with self.assertRaises(wire.WireError):
                wire.encode(wire.TYPE_PROBE, 1, 1, 1, 1, size)


class SchemaTests(unittest.TestCase):
    def test_ids(self):
        schema.validate_id("handoff-a_to_b.1")
        for bad in ("", "../x", "a/b", "-lead", "a b", "x" * 65, "a..b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                schema.validate_id(bad)
        self.assertTrue(schema.ID_RE.fullmatch(schema.new_run_id()))

    def test_schema_files_match_code(self):
        cols = json.loads((SCHEMAS / "csv_columns.json").read_text())
        for name, expected in schema.COLUMNS.items():
            self.assertEqual([c["name"] for c in cols[name]["columns"]], list(expected), name)
            ints = {c["name"] for c in cols[name]["columns"] if c["type"] == "int"}
            self.assertEqual(ints, schema.INT_COLUMNS[name], name)
        meta = json.loads((SCHEMAS / "run_meta.schema.json").read_text())
        self.assertEqual(set(meta["required"]), set(schema._META_REQUIRED))
        self.assertEqual(meta["properties"]["schema_version"]["const"], schema.SCHEMA_VERSION)
        json.loads((SCHEMAS / "metrics.schema.json").read_text())

    def test_validate_meta(self):
        from app.traffic.recorder import RunMeta
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            m = RunMeta(Path(d) / "m.json", "sender", "s", "r", 1, {}, {})
            self.assertEqual(schema.validate_meta(m.data), [])
            m.finalize("completed", "duration", {})
            self.assertEqual(schema.validate_meta(m.data), [])
            self.assertTrue(schema.validate_meta({"role": "x"}))
            broken = dict(m.data, status="completed", end_mono_ns=None)
            self.assertIn("finished run without end_mono_ns", schema.validate_meta(broken))


if __name__ == "__main__":
    unittest.main()
