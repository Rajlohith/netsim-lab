class HandshakeCompleted:
    def __init__(self, alpn_protocol=None):
        self.alpn_protocol = alpn_protocol


class StreamDataReceived:
    def __init__(self, data, end_stream, stream_id):
        self.data, self.end_stream, self.stream_id = data, end_stream, stream_id


class ConnectionTerminated:
    def __init__(self, error_code=0, frame_type=None, reason_phrase=""):
        self.error_code, self.frame_type, self.reason_phrase = error_code, frame_type, reason_phrase
