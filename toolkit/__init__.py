"""Reusable modelling code for run models (read-only during a run, like scripts/).

A run's model code imports it as a package; find the repo root from the file's
own location (a run lives under <repo>/runs/<run>/):

    import pathlib, sys
    sys.path.insert(0, next(str(p) for p in pathlib.Path(__file__).resolve().parents
                            if (p / "toolkit" / "alloc").is_dir()))
    from toolkit.alloc import costs, packages, optimize
"""
