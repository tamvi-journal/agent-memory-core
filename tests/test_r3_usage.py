import contextlib
import io
import pytest
from trajecta_identity.cli import parser
from tools.golden_cli.generate import usage_prefix


@pytest.mark.parametrize("argv,prog", [
    (["unknown-r3"], "trajecta-identity"),
    (["status", "leftover"], "trajecta-identity"),
    (["timeline", "--limit", "1.0"], "trajecta-identity timeline"),
    (["retrieve"], "trajecta-identity retrieve"),
    (["approve-core", "proposal", "--apply", "--reject"], "trajecta-identity approve-core"),
])
def test_usage_prefix_comes_from_actual_parser_not_argv(argv, prog):
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), pytest.raises(SystemExit) as exc:
        parser().parse_args(argv)
    assert exc.value.code == 2
    assert usage_prefix(stderr.getvalue().encode()) == f"{prog}: error:".encode()


def test_usage_prefix_refuses_unknown_prog():
    with pytest.raises(AssertionError):
        usage_prefix(b"usage\nunknown: error: synthetic\n")
