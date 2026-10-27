class QuicConfiguration:
    def __init__(self, is_client=True, alpn_protocols=None, verify_mode=None, connection_id_length=8, **kw):
        self.is_client, self.alpn_protocols, self.verify_mode = is_client, alpn_protocols or [], verify_mode
        self.connection_id_length = connection_id_length
        self.cert = None

    def load_cert_chain(self, certfile, keyfile=None, password=None):
        self.cert = (certfile, keyfile)
