"""Toy stand-in for aioquic.quic.connection.QuicConnection (sans-IO). Datagram types: 1 INITIAL, 2 HANDSHAKE, 3 DATA(seq,sid,bytes),
4 ACK(next_expected), 5 PING. Stream data is reliable and in order (retransmit every 100 ms of `now`)."""
from collections import deque

from . import events

RTO = 0.1


class QuicConnection:
    def __init__(self, configuration, original_destination_connection_id=None, **kw):
        self.is_client = configuration.is_client
        self._ev, self._out, self.peer = deque(), deque(), None
        self._seq, self._unacked, self._recv_next = 0, {}, 0
        self._timer = None
        self.cid_changes = self.pings_sent = 0
        self.peer_history = []              # server side: every distinct address the peer was seen at (migration)
        self.closed = False

    def connect(self, addr, now):
        self.peer = addr
        self._out.append((b"\x01", addr))

    def get_next_available_stream_id(self):
        return 0

    def change_connection_id(self):
        self.cid_changes += 1

    def send_ping(self, uid):
        self.pings_sent += 1
        self._out.append((b"\x05", self.peer))

    def send_stream_data(self, stream_id, data, end_stream=False):
        seq = self._seq
        self._seq += 1
        self._unacked[seq] = (stream_id, bytes(data))
        self._out.append((self._data(seq), self.peer))

    def _data(self, seq):
        sid, data = self._unacked[seq]
        return b"\x03" + seq.to_bytes(4, "big") + sid.to_bytes(4, "big") + data

    def close(self, *a, **k):
        self.closed = True

    def receive_datagram(self, data, addr, now):
        t = data[0]
        if not self.is_client and addr != self.peer:                    # server follows the client's current source address
            self.peer = addr
            self.peer_history.append(addr[0])
        if t == 1 and not self.is_client:
            self._out.append((b"\x02", addr))
            self._ev.append(events.HandshakeCompleted("stub"))
        elif t == 2 and self.is_client:
            self._ev.append(events.HandshakeCompleted("stub"))
        elif t == 3:
            seq, sid = int.from_bytes(data[1:5], "big"), int.from_bytes(data[5:9], "big")
            if seq == self._recv_next:
                self._recv_next += 1
                self._ev.append(events.StreamDataReceived(data[9:], False, sid))
            self._out.append((b"\x04" + self._recv_next.to_bytes(4, "big"), addr))
        elif t == 4:
            n = int.from_bytes(data[1:5], "big")
            for s in [s for s in self._unacked if s < n]:
                del self._unacked[s]
        elif t == 5:
            self._out.append((b"\x04" + self._recv_next.to_bytes(4, "big"), addr))

    def datagrams_to_send(self, now):
        out = list(self._out)
        self._out.clear()
        if self._unacked and self._timer is None:
            self._timer = now + RTO
        return out

    def get_timer(self):
        return self._timer

    def handle_timer(self, now):
        if self._timer is not None and now >= self._timer:
            for seq in sorted(self._unacked):
                self._out.append((self._data(seq), self.peer))
            self._timer = now + RTO if self._unacked else None

    def next_event(self):
        return self._ev.popleft() if self._ev else None
