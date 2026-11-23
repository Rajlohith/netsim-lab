"""Traffic generation and measurement.

Measurement code only: it knows nothing about mobility protocols. Protocol implementations
(protocol code) must live elsewhere and may only *use* these tools by running them as processes
or by reading their raw output files.
"""
