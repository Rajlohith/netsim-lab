"""Traffic generation and measurement (Stage 3).

Measurement code only: it knows nothing about mobility protocols. Protocol implementations
(later stages) must live elsewhere and may only *use* these tools by running them as processes
or by reading their raw output files.
"""
