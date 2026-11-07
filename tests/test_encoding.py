"""Hook I/O stays UTF-8 where the platform's default encoding is not.

On Windows before Python 3.15, pipes and files default to the ANSI code
page, cp1252 on Western systems. Each test runs the code in a child
interpreter that reproduces that on any platform: stdin's encoding is
forced to cp1252, and a text read or write that falls back on the default
encoding raises EncodingWarning as an error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import ROOT

# A closing curly quote is byte 0x9D in UTF-8, which cp1252 cannot decode,
# and "č" is not in cp1252 at all.
TEXT = "Tomaž asked for the “quoted” fix — č"


def run_child(code: str, tmp_path: Path, *, stdin: bytes = b"", env: dict | None = None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    child_env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252",
        "HOME": str(home),
        "USERPROFILE": str(home),
        **(env or {}),
    }
    child_env.pop("PYTHONUTF8", None)
    prelude = f"import sys; sys.path.insert(0, {str(ROOT / 'hooks')!r})\n"
    proc = subprocess.run(
        [
            sys.executable,
            "-X", "utf8=0",
            "-X", "warn_default_encoding",
            "-W", "error::EncodingWarning",
            "-c", prelude + textwrap.dedent(code),
        ],
        input=stdin,
        capture_output=True,
        env=child_env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return json.loads(proc.stdout.decode("utf-8"))


def test_the_payload_survives_a_cp1252_stdin(tmp_path):
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": TEXT}
    got = run_child(
        """
        import json, common
        sys.stdout.buffer.write(json.dumps(common.read_payload()).encode())
        """,
        tmp_path,
        stdin=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    assert got == payload


def test_the_env_file_is_read_as_utf8(tmp_path):
    env_file = tmp_path / ".env"
    # With the byte-order mark Notepad used to write.
    env_file.write_bytes(f"NEO4J_PASSWORD={TEXT}\nNEO4J_DATABASE=uam\n".encode("utf-8-sig"))
    got = run_child(
        f"""
        import json, common
        from pathlib import Path
        values = common._parse_env_file(Path({str(env_file)!r}))
        sys.stdout.buffer.write(json.dumps(values).encode())
        """,
        tmp_path,
    )
    assert got == {"NEO4J_PASSWORD": TEXT, "NEO4J_DATABASE": "uam"}


def test_the_account_email_is_read_from_a_config_with_non_ascii_text(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {f"C:/Users/{TEXT}": {}},
                "oauthAccount": {"emailAddress": "Maria@Company.com"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    got = run_child(
        """
        import json, common
        sys.stdout.buffer.write(json.dumps(common._claude_account_email()).encode())
        """,
        tmp_path,
        env={"CLAUDE_CONFIG_DIR": str(config)},
    )
    assert got == "maria@company.com"


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in CLI is a shebang script")
def test_the_claude_cli_round_trip_is_utf8(tmp_path):
    """A stand-in ``claude`` that echoes its stdin back as the result."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "text = sys.stdin.buffer.read().decode('utf-8')\n"
        "out = {'result': text, 'is_error': False}\n"
        "sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode('utf-8'))\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    got = run_child(
        f"""
        import json, llm
        result = llm._complete_claude_cli_once([{{"role": "user", "content": {TEXT!r}}}])
        sys.stdout.buffer.write(json.dumps(result).encode())
        """,
        tmp_path,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert got == TEXT
