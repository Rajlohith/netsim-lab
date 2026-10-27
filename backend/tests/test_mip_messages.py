import ipaddress
import unittest

from app.mobileip import messages as M
from tests.mip_helpers import AGENT, HOME, SA, raw_request

A = ipaddress.IPv4Address


class MessageTests(unittest.TestCase):
    def test_request_roundtrip_with_authentication(self):
        data, req = raw_request(lifetime=120)
        self.assertEqual(len(data), 24 + 6 + 32)                        # fixed part + ext header/SPI + HMAC-SHA256
        d = M.decode_request(data)
        self.assertEqual((d.flags, d.lifetime, str(d.home_address), str(d.home_agent), str(d.care_of_address)),
                         (M.FLAG_D, 120, HOME, AGENT, "10.10.4.2"))
        self.assertEqual((d.identification, d.spi), (req.identification, 256))
        self.assertTrue(SA.verify(d.signed, d.authenticator))
        self.assertEqual(d.signed, data[:-32])                          # authenticator covers everything before it

    def test_every_covered_byte_is_protected(self):
        data, _ = raw_request()
        for i in range(len(data) - 32):
            bad = bytearray(data)
            bad[i] ^= 0x01
            try:
                d = M.decode_request(bytes(bad))
            except M.MessageError:
                continue
            self.assertFalse(SA.verify(d.signed, d.authenticator), f"byte {i} not covered")

    def test_wrong_key_fails(self):
        other = M.SecurityAssociation(256, b"another-key-0123456789")
        data, _ = raw_request(sa=other)
        d = M.decode_request(data)
        self.assertFalse(SA.verify(d.signed, d.authenticator))

    def test_reply_roundtrip(self):
        rep = M.RegistrationReply(M.CODE_ACCEPTED, 30, A(HOME), A(AGENT), 12345)
        d = M.decode_reply(M.encode_reply(rep, SA))
        self.assertEqual((d.code, d.lifetime, d.identification), (0, 30, 12345))
        self.assertTrue(SA.verify(d.signed, d.authenticator))
        plain = M.decode_reply(M.encode_reply(rep, None))
        self.assertIsNone(plain.authenticator)

    def test_malformed_messages(self):
        data, _ = raw_request()
        for bad in (b"", data[:10], data[:24] + b"\x21\x24" + data[26:], data[:-1], data + b"\x00",
                    b"\x09" + data[1:], data[:24] + bytes([99]) + data[25:]):
            with self.subTest(len=len(bad)), self.assertRaises(M.MessageError):
                M.decode_request(bad)
        with self.assertRaises(M.MessageError):
            M.decode_reply(b"\x01" * 30)

    def test_security_association_validation(self):
        with self.assertRaises(ValueError):
            M.SecurityAssociation(10, b"k" * 20)                         # reserved SPI
        with self.assertRaises(ValueError):
            M.SecurityAssociation(256, b"short")

    def test_identification_is_ntp_timestamp(self):
        ns = 1_800_000_000_500_000_000
        i = M.ident_from_wall_ns(ns)
        self.assertEqual(i >> 32, 1_800_000_000 + M.NTP_UNIX_OFFSET)
        self.assertAlmostEqual(M.wall_ns_from_ident(i), ns, delta=2)
        self.assertGreater(M.ident_from_wall_ns(ns + 1_000_000), i)


if __name__ == "__main__":
    unittest.main()
