"""The garden fixture harness (run-tests.sh) run end to end under pytest.

Nothing else collects the shell harness, so without this test a broken
run-tests.sh stays broken silently. The harness builds its own throwaway
fixture wiki; GARDEN_FIXTURE_DIR points that build at pytest's tmp_path
so cleanup is pytest's. The deadline is Python-enforced (subprocess
timeout), never a `timeout` binary, which not every platform ships.

The environment is stripped of every git identity source (config files
and author/committer variables) so the run proves the harness works on a
bare CI runner: the installer's initial commit carries its own inline
`-c user.name/user.email` and must not lean on ambient config.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[2]
HARNESS = KIT_ROOT / "tests" / "garden" / "run-tests.sh"

GIT_IDENTITY_VARS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "EMAIL",
)


def test_harness_runs_end_to_end(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in GIT_IDENTITY_VARS
    }
    env.update(
        GARDEN_FIXTURE_DIR=str(tmp_path),
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
    )
    # start_new_session puts the harness and every descendant (installer,
    # index builder) in one process group so an expired deadline kills the
    # whole tree, not just the bash parent.
    with subprocess.Popen(
        ["bash", str(HARNESS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
    assert proc.returncode == 0, stdout + stderr
    assert "deterministic tests passed" in stdout
    # The harness built and used the throwaway fixture, not the kit root.
    fixture = tmp_path / "fixture-wiki"
    assert (fixture / "wiki.toml").is_file()
    assert (fixture / "workstreams" / "usage-budget-monitoring.md").is_file()
    assert not (KIT_ROOT / "workstreams").exists()
