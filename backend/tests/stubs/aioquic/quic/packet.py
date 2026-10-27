class _Header:
    destination_cid = b"stub-cid"


def pull_quic_header(buf, host_cid_length=None):
    return _Header()
