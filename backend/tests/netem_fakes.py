"""In-memory stand-in for the host: simulates `ip netns` so logic can be tested without root."""
from app.netem.runner import CmdResult

SAMPLE_PING = """PING 10.10.1.2 (10.10.1.2) 56(84) bytes of data.

--- 10.10.1.2 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 800ms
rtt min/avg/max/mdev = 10.100/10.500/11.000/0.300 ms
"""


class FakeHost:
    def __init__(self, namespaces=(), fail_on_call=None):
        self.namespaces = set(namespaces)
        self.calls = []
        self.fail_on_call = fail_on_call   # 1-based index of the call that should fail
        self.ping_output = SAMPLE_PING

    def __call__(self, argv, timeout):
        if argv[:2] == ["sudo", "-n"]:
            argv = argv[2:]
        self.calls.append(tuple(argv))
        if self.fail_on_call == len(self.calls):
            return CmdResult(tuple(argv), 1, "", "injected failure")
        if argv[1:3] == ["netns", "list"]:
            return CmdResult(tuple(argv), 0, "".join(f"{n} (id: 0)\n" for n in sorted(self.namespaces)))
        if argv[1:3] == ["netns", "add"]:
            if argv[3] in self.namespaces:
                return CmdResult(tuple(argv), 1, "", "exists")
            self.namespaces.add(argv[3])
            return CmdResult(tuple(argv), 0)
        if argv[1:3] == ["netns", "del"]:
            if argv[3] not in self.namespaces:
                return CmdResult(tuple(argv), 1, "", "no such ns")
            self.namespaces.discard(argv[3])
            return CmdResult(tuple(argv), 0)
        ns = argv[3] if argv[1:3] == ["netns", "exec"] else argv[2]
        if ns not in self.namespaces:
            return CmdResult(tuple(argv), 1, "", f"Cannot open network namespace {ns}")
        if "ping" in argv:
            return CmdResult(tuple(argv), 0, self.ping_output)
        return CmdResult(tuple(argv), 0, "")

    def mutating(self):
        return [c for c in self.calls if c[1:3] != ("netns", "list")]
