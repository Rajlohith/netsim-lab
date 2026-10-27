def test_scientific_stack_imports_and_works():
    import matplotlib
    import numpy as np
    import pandas as pd
    from sklearn.linear_model import LinearRegression

    matplotlib.use("Agg")  # headless
    x = np.arange(10).reshape(-1, 1)
    df = pd.DataFrame({"x": x.ravel(), "y": 2 * x.ravel() + 1})
    model = LinearRegression().fit(df[["x"]], df["y"])
    assert abs(model.coef_[0] - 2) < 1e-9
