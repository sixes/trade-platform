import importlib.util
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest


CREDENTIALS = (
    "LONGPORT_APP_KEY",
    "LONGPORT_APP_SECRET",
    "LONGPORT_ACCESS_TOKEN",
)
PROFILE_VALUES = dict(zip(CREDENTIALS, ("fake-profile-key", "fake-profile-secret", "fake-profile-token")))
STALE_VALUES = dict(zip(CREDENTIALS, ("fake-stale-key", "fake-stale-secret", "fake-stale-token")))
DOTENV_VALUES = dict(zip(CREDENTIALS, ("fake-dotenv-key", "fake-dotenv-secret", "fake-dotenv-token")))


def profile_text(values):
    return "".join(f"export {name}={shlex.quote(value)}\n" for name, value in values.items())


def credentials():
    return {name: os.environ.get(name) for name in CREDENTIALS}


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    # Replace, rather than copy, the process environment: never inspect real credentials.
    monkeypatch.setattr(os, "environ", {"HOME": str(home)})

    import dotenv

    dotenv_calls = []

    def fake_load_dotenv(path, *, override):
        dotenv_calls.append((path, override, dict(os.environ)))
        for name, value in DOTENV_VALUES.items():
            if override or name not in os.environ:
                os.environ[name] = value
        return True

    monkeypatch.setattr(dotenv, "load_dotenv", fake_load_dotenv)

    def load_config():
        # A fresh module avoids both cached imports and changing another test's config.
        # HOME and dotenv are patched before any backend config code can execute.
        source = Path(__file__).resolve().parents[1] / "backend" / "config.py"
        spec = importlib.util.spec_from_file_location("_config_under_test", source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return SimpleNamespace(
        profile=home / ".bashrc",
        load_config=load_config,
        dotenv_calls=dotenv_calls,
    )


def test_startup_profile_overrides_stale_environment_before_dotenv(sandbox):
    os.environ.update(STALE_VALUES)
    sandbox.profile.write_text(profile_text(PROFILE_VALUES), encoding="utf-8")

    config = sandbox.load_config()

    assert credentials() == PROFILE_VALUES
    assert sandbox.dotenv_calls == [
        (config.PROJECT_ROOT / ".env", False, {"HOME": str(sandbox.profile.parent), **PROFILE_VALUES})
    ]


@pytest.mark.parametrize("existing", [False, True], ids=["dotenv-fallback", "existing-env"])
@pytest.mark.parametrize(
    "text",
    [None, "", "# No credentials here.\n", "export UNRELATED_CONFIG_TEST=fake-unrelated\n"],
    ids=["absent-file", "empty-file", "comments-only", "unrelated-export"],
)
def test_absent_canonical_group_preserves_fallback(sandbox, existing, text):
    if existing:
        os.environ.update(STALE_VALUES)
    before = dict(os.environ)
    if text is not None:
        sandbox.profile.write_text(text, encoding="utf-8")

    config = sandbox.load_config()

    expected = STALE_VALUES if existing else DOTENV_VALUES
    assert sandbox.dotenv_calls == [(config.PROJECT_ROOT / ".env", False, before)]
    assert dict(os.environ) == {**before, **expected}
    after = dict(os.environ)
    assert config.load_longport_profile(sandbox.profile) is None
    assert dict(os.environ) == after


def test_literals_preserve_equals_spaces_and_quoted_hashes(sandbox):
    sandbox.profile.write_text(
        " \texport\tLONGPORT_APP_KEY=fake-key=part==\n"
        "export LONGPORT_APP_SECRET='fake secret=#part' # ignored comment\n"
        'export LONGPORT_ACCESS_TOKEN="fake token=#part=="\t# another comment\n',
        encoding="utf-8",
    )

    sandbox.load_config()

    assert credentials() == {
        "LONGPORT_APP_KEY": "fake-key=part==",
        "LONGPORT_APP_SECRET": "fake secret=#part",
        "LONGPORT_ACCESS_TOKEN": "fake token=#part==",
    }


@pytest.mark.parametrize("existing", [False, True], ids=["unset-env", "stale-env"])
@pytest.mark.parametrize(
    "present",
    [(0,), (1,), (2,), (0, 1), (0, 2), (1, 2)],
    ids=["key-only", "secret-only", "token-only", "no-token", "no-secret", "no-key"],
)
def test_incomplete_group_is_atomic_and_sanitized(sandbox, existing, present):
    config = sandbox.load_config()
    for name in CREDENTIALS:
        os.environ.pop(name, None)
    if existing:
        os.environ.update(STALE_VALUES)
    before = dict(os.environ)
    sandbox.profile.write_text(
        profile_text({CREDENTIALS[index]: PROFILE_VALUES[CREDENTIALS[index]] for index in present}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError) as error:
        config.load_longport_profile(sandbox.profile)

    assert dict(os.environ) == before
    assert str(error.value) == (
        "Incomplete Longbridge exports in ~/.bashrc; set LONGPORT_APP_KEY, "
        "LONGPORT_APP_SECRET and LONGPORT_ACCESS_TOKEN together"
    )
    assert error.value.__cause__ is None


@pytest.mark.parametrize("existing", [False, True], ids=["unset-env", "stale-env"])
@pytest.mark.parametrize(
    ("assignment", "reason"),
    [
        ("=''", "Empty"),
        ('=""', "Empty"),
        ("=", "Invalid"),
        ("", "Invalid"),
        (" =fake-rejected-token", "Invalid"),
        ("= fake-rejected-token", "Invalid"),
        ("='fake-rejected-token", "Invalid"),
        ('="fake-rejected-token', "Invalid"),
        ("=fake-rejected-token extra", "Invalid"),
        ("=fake-rejected-token; :", "Invalid"),
        ("=$FAKE_REJECTED_TOKEN", "Invalid"),
        ('="${FAKE_REJECTED_TOKEN}"', "Invalid"),
        ("=$(printf fake-rejected-token)", "Invalid"),
        ('="$(printf fake-rejected-token)"', "Invalid"),
        ("=`printf fake-rejected-token`", "Invalid"),
        ('="fake\\rejected-token"', "Invalid"),
        ("='fake\x00rejected-token'", "Invalid"),
    ],
)
def test_invalid_token_is_atomic_and_sanitized(sandbox, existing, assignment, reason):
    config = sandbox.load_config()
    for name in CREDENTIALS:
        os.environ.pop(name, None)
    if existing:
        os.environ.update(STALE_VALUES)
    before = dict(os.environ)
    # Valid preceding assignments must not leak into the environment on failure.
    sandbox.profile.write_text(
        profile_text({name: PROFILE_VALUES[name] for name in CREDENTIALS[:2]})
        + f"export LONGPORT_ACCESS_TOKEN{assignment}\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError) as error:
        config.load_longport_profile(sandbox.profile)

    assert dict(os.environ) == before
    # Exact messages exclude the rejected input, earlier values and disposable path.
    assert str(error.value) == f"{reason} literal export for LONGPORT_ACCESS_TOKEN in ~/.bashrc"
    assert error.value.__cause__ is None


def test_bad_profile_aborts_startup_without_dotenv_or_partial_update(sandbox):
    os.environ.update(STALE_VALUES)
    before = dict(os.environ)
    sandbox.profile.write_text(
        profile_text({name: PROFILE_VALUES[name] for name in CREDENTIALS[:2]})
        + 'export LONGPORT_ACCESS_TOKEN="$(printf fake-rejected-token)"\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError) as error:
        sandbox.load_config()

    assert str(error.value) == "Invalid literal export for LONGPORT_ACCESS_TOKEN in ~/.bashrc"
    assert dict(os.environ) == before
    assert sandbox.dotenv_calls == []


def test_unrelated_shell_commands_are_not_executed(sandbox):
    marker = sandbox.profile.parent / "must-not-exist"
    sandbox.profile.write_text(
        f"touch {shlex.quote(str(marker))}\n"
        "export UNRELATED_CONFIG_TEST=fake-shell-value\n"
        "exit 1\n"
        + profile_text(PROFILE_VALUES),
        encoding="utf-8",
    )

    sandbox.load_config()

    assert not marker.exists()
    assert credentials() == PROFILE_VALUES
    assert "UNRELATED_CONFIG_TEST" not in os.environ


@pytest.mark.parametrize("existing", [False, True], ids=["dotenv-fallback", "existing-env"])
def test_aliases_and_non_export_assignments_are_ignored(sandbox, existing):
    if existing:
        os.environ.update(STALE_VALUES)
    before = dict(os.environ)
    sandbox.profile.write_text(
        "export LONGBRIDGE_APP_KEY=fake-alias-key\n"
        "export LONGBRIDGE_APP_SECRET=fake-alias-secret\n"
        "export LONGBRIDGE_ACCESS_TOKEN=fake-alias-token\n"
        "export LONGPORT_KEY=fake-short-alias-key\n"
        "export LONGPORT_SECRET=fake-short-alias-secret\n"
        "export LONGPORT_TOKEN=fake-short-alias-token\n"
        "# export LONGPORT_APP_KEY=fake-comment-key\n"
        "LONGPORT_APP_KEY=fake-unexported-key\n"
        "LONGPORT_APP_SECRET=fake-unexported-secret\n"
        "LONGPORT_ACCESS_TOKEN=fake-unexported-token\n",
        encoding="utf-8",
    )

    config = sandbox.load_config()

    assert sandbox.dotenv_calls == [(config.PROJECT_ROOT / ".env", False, before)]
    assert dict(os.environ) == {**before, **(STALE_VALUES if existing else DOTENV_VALUES)}


@pytest.mark.parametrize("startup", [False, True], ids=["explicit-load", "another-startup"])
def test_updated_profile_is_read_again(sandbox, startup):
    sandbox.profile.write_text(profile_text(PROFILE_VALUES), encoding="utf-8")
    config = sandbox.load_config()
    assert credentials() == PROFILE_VALUES
    updated = {name: f"{value}-updated" for name, value in PROFILE_VALUES.items()}
    sandbox.profile.write_text(profile_text(updated), encoding="utf-8")

    if startup:
        sandbox.load_config()
    else:
        assert config.load_longport_profile(sandbox.profile) is None

    assert credentials() == updated
    assert len(sandbox.dotenv_calls) == (2 if startup else 1)
    if startup:
        assert sandbox.dotenv_calls[-1] == (
            config.PROJECT_ROOT / ".env", False, {"HOME": str(sandbox.profile.parent), **updated}
        )
