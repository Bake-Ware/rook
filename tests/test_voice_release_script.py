"""release.sh input validation: tags, release paths, refs, fetch failure and the
candidate pid check. Runs only the subcommands that need no systemd or sudo."""
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'services' / 'voice' / 'deploy' / 'release.sh'
pytestmark = pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')


def release(tmp_path, *args, **env):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    full = {'PATH': os.environ['PATH'], 'HOME': str(home), 'VOICE_HOME': str(tmp_path / 'voice'),
            'VOICE_REPO': str(tmp_path / 'no-repo'), 'VOICE_PYTHON': 'python3', 'GIT_CONFIG_NOSYSTEM': '1', **env}
    return subprocess.run(['bash', str(SCRIPT), *args], capture_output=True, text=True, env=full, timeout=60)


def make_release(root, tag):
    (root / tag / 'services' / 'voice').mkdir(parents=True)
    (root / tag / 'services' / 'voice' / 'server.py').write_text('')
    return root / tag


@pytest.mark.parametrize('tag', ['v2026.10.06', 'abc1234', 'voice-2026-10-06', 'A_b.c-1'])
def test_valid_tags(tmp_path, tag):
    result = release(tmp_path, 'validate-tag', tag)
    assert result.returncode == 0 and result.stdout.strip() == 'ok'


@pytest.mark.parametrize('tag', ['', '..', '.hidden', '-rf', '../x', 'a/b', 'a b', 'a;b', '$(id)', 'x\ny', '_x'])
def test_invalid_tags(tmp_path, tag):
    result = release(tmp_path, 'validate-tag', tag)
    assert result.returncode != 0 and 'invalid release tag' in result.stderr


def test_resolve_accepts_a_release_inside_releases(tmp_path):
    releases = tmp_path / 'voice' / 'releases'
    good = make_release(releases, 'good')
    result = release(tmp_path, 'resolve', 'good')
    assert result.returncode == 0 and result.stdout.strip() == str(good.resolve())


def test_symlink_escape_is_refused_for_every_tag_command(tmp_path):
    releases = tmp_path / 'voice' / 'releases'
    releases.mkdir(parents=True)
    outside = make_release(tmp_path / 'elsewhere', 'evil')
    (releases / 'evil').symlink_to(outside)
    for command in ('resolve', 'test', 'candidate', 'activate'):
        result = release(tmp_path, command, 'evil')
        assert result.returncode != 0, command
        assert 'resolves outside' in result.stderr, (command, result.stderr)


def test_missing_or_non_release_directory_is_refused(tmp_path):
    releases = tmp_path / 'voice' / 'releases'
    (releases / 'empty').mkdir(parents=True)
    assert 'no release' in release(tmp_path, 'resolve', 'absent').stderr
    assert 'no voice release' in release(tmp_path, 'resolve', 'empty').stderr


def test_build_validates_tag_and_ref_before_touching_git(tmp_path):
    result = release(tmp_path, 'build', 'master', '../escape')
    assert result.returncode != 0 and 'invalid release tag' in result.stderr
    result = release(tmp_path, 'build', '--upload-pack=touch /tmp/x')
    assert result.returncode != 0 and 'invalid ref' in result.stderr


def test_build_fails_when_fetch_fails(tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', str(tmp_path / 'missing-remote')], check=True)
    result = release(tmp_path, 'build', 'master', VOICE_REPO=str(repo))
    assert result.returncode != 0 and 'git fetch failed' in result.stderr
    assert not (tmp_path / 'voice' / 'releases').exists() or not any((tmp_path / 'voice' / 'releases').iterdir())


def test_build_resolves_origin_branch_and_prints_sha(tmp_path):
    remote, repo = tmp_path / 'remote', tmp_path / 'repo'
    env = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
           'HOME': str(tmp_path), 'PATH': os.environ['PATH'], 'GIT_CONFIG_NOSYSTEM': '1'}
    def git(*args, cwd=remote):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()
    subprocess.run(['git', 'init', '-q', '-b', 'master', str(remote)], check=True, env=env)
    (remote / 'services' / 'voice').mkdir(parents=True)
    (remote / 'services' / 'voice' / 'server.py').write_text('VERSION = 1\n')
    git('add', '.'); git('commit', '-qm', 'one')
    sha = git('rev-parse', 'HEAD')
    subprocess.run(['git', 'clone', '-q', str(remote), str(repo)], check=True, env=env)
    # A local branch with the same name but other content must never be used.
    git('checkout', '-qb', 'master-local', cwd=repo)
    result = release(tmp_path, 'build', 'master', 'rel1', VOICE_REPO=str(repo))
    assert result.returncode == 0, result.stderr
    assert f'resolved master -> {sha}' in result.stdout
    assert (tmp_path / 'voice' / 'releases' / 'rel1' / 'REVISION').read_text().startswith(f'commit {sha}')
    result = release(tmp_path, 'build', 'no-such-branch', VOICE_REPO=str(repo))
    assert result.returncode != 0 and 'unknown ref' in result.stderr


def test_candidate_stop_does_not_kill_a_stale_pid(tmp_path):
    candidate = tmp_path / 'voice' / 'releases' / '.candidate'
    candidate.mkdir(parents=True)
    bystander = subprocess.Popen(['sleep', '30'])
    try:
        (candidate / 'pid').write_text(str(bystander.pid))
        (candidate / 'pid-start').write_text('1')          # not this process's start time
        (candidate / 'runner').write_text('pid')
        (candidate / 'release').write_text(str(tmp_path))
        result = release(tmp_path, 'candidate-stop')
        assert result.returncode == 0 and 'nothing killed' in result.stdout
        time.sleep(.2)
        assert bystander.poll() is None
        assert not candidate.exists()
        # Even with the right start time, an unrelated command line is not killed.
        candidate.mkdir()
        start = Path(f'/proc/{bystander.pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
        (candidate / 'pid').write_text(str(bystander.pid))
        (candidate / 'pid-start').write_text(start)
        (candidate / 'runner').write_text('pid')
        (candidate / 'release').write_text(str(tmp_path))
        result = release(tmp_path, 'candidate-stop')
        assert 'nothing killed' in result.stdout
        time.sleep(.2)
        assert bystander.poll() is None
    finally:
        bystander.kill(); bystander.wait()


def test_candidate_defaults_to_cpu_for_stt_and_tts():
    import importlib.util
    spec = importlib.util.spec_from_file_location('voice_candidate', SCRIPT.parent / 'candidate.py')
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)
    assert candidate.candidate_devices() == {'WHISPER_DEVICE': 'cpu', 'WHISPER_COMPUTE': 'int8',
                                             'ONNX_PROVIDER': 'CPUExecutionProvider',
                                             'VOICE_CHATTERBOX_DEVICE': ''}
    assert candidate.candidate_devices(live_devices=True) == {}


def test_voice_select_refuses_without_root(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('running as root')
    result = subprocess.run(['bash', str(SCRIPT.parent / 'voice-select'), 'activate', 'x'],
                            capture_output=True, text=True, env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path)})
    assert result.returncode != 0 and 'must run as root' in result.stderr
