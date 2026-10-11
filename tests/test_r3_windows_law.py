"""P19/P20 use isolated public paths; CRLF tolerance is last-profile only."""
from pathlib import Path
import shutil
import subprocess

from tools.r3.common import isolated_env
from tools.r3.run import run_case

ROOT = Path(__file__).resolve().parents[1]


def test_last_profile_writer_requests_lf_explicitly(tmp_path, monkeypatch):
    from trajecta_identity.recipe import remember
    env = isolated_env(tmp_path)
    calls = []
    write = Path.write_text

    def observed(path, data, **kwargs):
        calls.append((path.name, data, kwargs))
        return write(path, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", observed)
    remember("companion", env)
    assert calls == [(".last-profile", "companion\n", {"encoding": "utf-8", "newline": "\n"})]
    assert (tmp_path / "data/.last-profile").read_bytes() == b"companion\n"


def test_public_cli_writes_identical_lf_last_profile_in_both_runtimes(tmp_path):
    values = []
    for runtime in ("py", "ts"):
        root = tmp_path / runtime
        isolated_env(root)
        out = run_case(runtime, root, {"surface": "cli", "argv": [
            "--profile", "companion", "--db", str(root / "store.sqlite3"), "status",
        ]})
        assert out["exit"] == 0 and not out["stderr"], out
        assert not (root / "store.sqlite3").exists()
        values.append((root / "data/.last-profile").read_bytes())
    assert values == [b"companion\n", b"companion\n"]


def test_legacy_crlf_last_profile_resolves_without_rewriting_in_both_runtimes(tmp_path):
    from trajecta_identity.recipe import last_profile, resolve
    env = isolated_env(tmp_path)
    path = tmp_path / "data/.last-profile"
    path.write_bytes(b"companion\r\n")
    assert last_profile(env) == "companion"
    assert resolve(env=env, remember_choice=False).name == "companion"
    module = (ROOT / "node/src/recipe.ts").as_uri()
    program = f'import {{lastProfile, resolveProfile}} from {module!r};\n'
    program += 'if(lastProfile()!=="companion")throw Error("legacy name differs");\n'
    program += 'console.log((await resolveProfile(undefined,{rememberChoice:false})).name);'
    result = subprocess.run([shutil.which("node"), "--experimental-strip-types", "--no-warnings=ExperimentalWarning", "--input-type=module", "-e", program],
                            cwd=tmp_path, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"companion\n"
    assert not result.stderr
    assert path.read_bytes() == b"companion\r\n"
