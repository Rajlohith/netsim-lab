import ipaddress
import unittest

from app.mobileip import ipip


def inner(payload=64, ttl=64, df=False, tos=0):
    return ipip.build_udp_ip("10.10.1.2", "10.10.100.2", 40000, 5005, b"x" * payload, ttl=ttl, df=df)


class IpIpTests(unittest.TestCase):
    def test_overhead_is_exactly_one_ipv4_header(self):
        p = inner()
        o = ipip.encapsulate(p, "10.10.100.1", "10.10.4.2")
        self.assertEqual(len(o) - len(p), ipip.OVERHEAD)
        h, _ = ipip.parse_ipv4(o)
        self.assertEqual((h.protocol, str(h.src), str(h.dst), h.ttl), (ipip.PROTO_IPIP, "10.10.100.1", "10.10.4.2", 64))

    def test_roundtrip_and_inner_ttl_decrement(self):
        p = inner(ttl=64)
        outer_h, back = ipip.decapsulate(ipip.encapsulate(p, "10.10.100.1", "10.10.4.2"))
        ih, ipayload = ipip.parse_ipv4(back)
        self.assertEqual(ih.ttl, 63)                                  # encapsulator acted as a router
        self.assertEqual((str(ih.src), str(ih.dst)), ("10.10.1.2", "10.10.100.2"))
        _, _, data = (None, None, ipip.parse_udp(ih.src, ih.dst, ipayload)[2])
        self.assertEqual(data, b"x" * 64)

    def test_ttl_exhausted_inner_is_not_encapsulated(self):
        with self.assertRaises(ipip.IPError):
            ipip.encapsulate(inner(ttl=1), "10.10.100.1", "10.10.4.2")
        ipip.encapsulate(inner(ttl=1), "10.10.100.1", "10.10.4.2", decrement_inner_ttl=False)

    def test_df_and_tos_are_copied_to_outer(self):
        p = ipip.build_ipv4("10.10.1.2", "10.10.100.2", 17, b"abcd", tos=0x2E, df=True)
        h, _ = ipip.parse_ipv4(ipip.encapsulate(p, "10.10.100.1", "10.10.4.2"))
        self.assertTrue(h.df)
        self.assertEqual(h.tos, 0x2E)

    def test_corruption_is_detected(self):
        o = bytearray(ipip.encapsulate(inner(), "10.10.100.1", "10.10.4.2"))
        for idx in (1, 8, 12, 19):                                     # flip a bit in the outer header
            bad = bytearray(o)
            bad[idx] ^= 0x01
            with self.subTest(byte=idx), self.assertRaises(ipip.IPError):
                ipip.decapsulate(bytes(bad))
        with self.assertRaises(ipip.IPError):
            ipip.decapsulate(bytes(o[:30]))                            # truncated
        bad_inner = bytearray(o)
        bad_inner[20 + 12] ^= 0x01                                     # inner header corrupted, outer fine
        with self.assertRaises(ipip.IPError):
            ipip.decapsulate(bytes(bad_inner))

    def test_non_ipip_and_fragments_rejected(self):
        with self.assertRaises(ipip.IPError):
            ipip.decapsulate(inner())                                  # plain UDP packet
        frag = bytearray(ipip.build_ipv4("1.1.1.1", "2.2.2.2", 4, inner()))
        frag[6] |= 0x20                                                # MF
        frag[10:12] = b"\x00\x00"
        frag[10:12] = ipip.checksum(bytes(frag[:20])).to_bytes(2, "big")
        with self.assertRaises(ipip.IPError):
            ipip.decapsulate(bytes(frag))

    def test_udp_checksum_roundtrip_and_tamper(self):
        p = inner()
        h, pl = ipip.parse_ipv4(p)
        self.assertEqual(ipip.parse_udp(h.src, h.dst, pl)[:2], (40000, 5005))
        bad = bytearray(pl)
        bad[-1] ^= 0xFF
        with self.assertRaises(ipip.IPError):
            ipip.parse_udp(h.src, h.dst, bytes(bad))

    def test_checksum_known_value(self):
        hdr = bytes.fromhex("450000730000400040110000c0a80001c0a800c7")   # classic worked example
        self.assertEqual(ipip.checksum(hdr), 0xB861)


if __name__ == "__main__":
    unittest.main()
