"""Stage 5: controlled comparison of mobility approaches behind one experiment interface.

Every approach is an adapter that produces the same raw files (Stage 3 probe files + approach_events.csv +
experiment_summary.json); one normaliser computes the same core metrics from them. Simplified models are labelled
as such everywhere (see docs/MOBILITY_COMPARISON.md); unsupported combinations are reported, never silently skipped.
"""
