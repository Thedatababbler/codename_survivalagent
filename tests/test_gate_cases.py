"""Cases read from the source, outcomes over parametrised runs, exact dropping of soft / vacuous cases."""

from adamas_unit.gate import GateResult, case_ids, complete, drop, outcome
from adamas_unit.import_suites import _case_of

SUITE = '''
import deepdiff                      # top-level import: the suite does not collect without the package

def test_a():
    pass

def test_ab():
    pass

class TestK:
    def test_m(self):
        pass
    def helper(self):
        pass

def helper():
    pass
'''


def test_case_ids_from_source(tmp_path):
    (tmp_path / "test_x.py").write_text(SUITE)
    assert case_ids(tmp_path, "u") == ["u/test_x.py::TestK::test_m", "u/test_x.py::test_a", "u/test_x.py::test_ab"]


def test_outcome_over_parametrised_runs():
    r = {"u/t.py::test_a[1]": "passed", "u/t.py::test_a[2]": "failed", "u/t.py::test_ab": "passed"}
    assert outcome(r, "u/t.py::test_a") == "failed"
    assert outcome(r, "u/t.py::test_ab") == "passed"
    assert outcome({"u/t.py::test_a[1]": "passed"}, "u/t.py::test_a") == "passed"
    assert outcome(r, "u/t.py::test_zz") == "missing"


def test_complete_counts_unreported_cases_only():
    g = complete(GateResult(results={"u/t.py::test_a[1]": "passed"}), ["u/t.py::test_a", "u/t.py::test_b"])
    assert g.results == {"u/t.py::test_a[1]": "passed", "u/t.py::test_b": "missing"}


def test_drop_is_exact_not_prefix():
    g = drop(GateResult(results={"u/t.py::test_a": "failed", "u/t.py::test_a[1]": "failed", "u/t.py::test_ab": "failed"}),
             ["u/t.py::test_a"])
    assert g.results == {"u/t.py::test_ab": "failed"}


def test_case_of():
    assert _case_of("u/test_x.py::TestK::test_m[p]") == ("test_x.py", "test_m")
