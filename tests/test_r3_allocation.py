import pytest

from tools.r3.allocation_regression import run


@pytest.mark.parametrize("holder,victim", [("py", "ts"), ("ts", "py")])
@pytest.mark.parametrize("operation", ["close-loop", "revise"])
def test_bidirectional_allocation_equals_serial_order(tmp_path, holder, victim, operation):
    run(tmp_path, holder, victim, operation)
