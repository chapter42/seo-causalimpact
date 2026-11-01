"""Runs tfcausalimpact in a separate process.

TensorFlow can deadlock inside Streamlit's script thread (and on some local
TF/TFP versions). Running it in a child process lets the app enforce a timeout
and fall back to the statsmodels engine.

Usage: python -m core._tfci_worker <in.pkl> <out.pkl>
"""

import pickle
import sys


def main(src: str, dst: str) -> None:
    with open(src, "rb") as fh:
        job = pickle.load(fh)
    from causalimpact import CausalImpact  # tfcausalimpact

    ci = CausalImpact(job["data"], job["pre_period"], job["post_period"], alpha=job["alpha"])
    out = {
        "inferences": ci.inferences,
        "summary_data": ci.summary_data,
        "p_value": float(getattr(ci, "p_value", float("nan"))),
    }
    with open(dst, "wb") as fh:
        pickle.dump(out, fh)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
