def test_scientific_stack_imports_and_works():
    import matplotlib
    import numpy as np
    import pandas as pd
    import scipy.stats

    matplotlib.use("Agg")  # headless
    x = np.arange(10)
    df = pd.DataFrame({"x": x, "y": 2 * x + 1})
    fit = scipy.stats.linregress(df["x"], df["y"])
    assert abs(fit.slope - 2) < 1e-9
