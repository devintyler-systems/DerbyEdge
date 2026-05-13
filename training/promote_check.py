"""
training/promote_check.py

Thin CLI entry point that runs the full evaluation + promotion check.
Equivalent to: python -m training.evaluate_shadow_vs_baseline

Usage
-----
    python -m training.promote_check
    python -m training.promote_check --eval-file output/shadow_eval.csv
    python -m training.promote_check --out-dir output/eval_20260512
"""
from training.evaluate_shadow_vs_baseline import main

if __name__ == "__main__":
    main()
