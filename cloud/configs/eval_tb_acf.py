"""Eval suite (Phase 50 α): question `tb_acf` (synthetic; see docs/design/eval-suite.md)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _eval import eval_config  # noqa: E402

CONFIG = eval_config("tb_acf")
