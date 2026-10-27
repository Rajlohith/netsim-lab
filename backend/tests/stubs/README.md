# STUB `aioquic` (test double, NOT the real library)

Implements only the small API surface `app/compare/quic_sim.py` uses, with a toy reliable-stream protocol (3 datagram types,
retransmit timer, server follows the client's new source address immediately). It exists to run the adapter's own glue code
(event dispatch, timers, source-address switching, stream wiring) in an environment where the real library cannot be installed.
It proves nothing about real aioquic or real QUIC behaviour. Used only by `tests/test_compare_quic_glue.py`, which is skipped
whenever the real aioquic is installed.
