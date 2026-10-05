"""Profile path resolution: the legacy fallback (no active profile) and
the active-profile override, both driven entirely off module-level
constants so they're easy to redirect into a tmp_path per test."""
import profile as profile_mod


def test_sanitize_label_strips_and_truncates():
    assert profile_mod.sanitize_label("Rich@rd Eseke!!") == "RichrdEseke"
    assert profile_mod.sanitize_label("a" * 30) == "a" * profile_mod.MAX_LABEL_LEN
    assert profile_mod.sanitize_label("") == ""


def test_active_profile_id_prefers_env_var(monkeypatch, tmp_path):
    marker = tmp_path / ".active_profile"
    marker.write_text("FROM-MARKER\n")
    monkeypatch.setattr(profile_mod, "ACTIVE_MARKER", marker)
    monkeypatch.setenv("JOB_ASSIST_PROFILE", "FROM-ENV")
    assert profile_mod.active_profile_id() == "FROM-ENV"


def test_active_profile_id_falls_back_to_marker_file(monkeypatch, tmp_path):
    marker = tmp_path / ".active_profile"
    marker.write_text("FROM-MARKER\n")
    monkeypatch.setattr(profile_mod, "ACTIVE_MARKER", marker)
    monkeypatch.delenv("JOB_ASSIST_PROFILE", raising=False)
    assert profile_mod.active_profile_id() == "FROM-MARKER"


def test_path_resolvers_fall_back_to_legacy_with_no_active_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(profile_mod, "ACTIVE_MARKER", tmp_path / "missing")
    monkeypatch.delenv("JOB_ASSIST_PROFILE", raising=False)
    assert profile_mod.job_state_db_path() is None
    assert profile_mod.config_path() == profile_mod.BASE / "config.local.json"


def test_create_profile_copies_skeleton(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    skeleton = profiles_dir / "_skeleton"
    skeleton.mkdir(parents=True)
    (skeleton / "config.json").write_text("{}")
    monkeypatch.setattr(profile_mod, "PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(profile_mod, "SKELETON_DIR", skeleton)

    pid = profile_mod.create_profile("Test User")
    assert pid == "TestUser"
    assert (profiles_dir / pid / "config.json").exists()


def test_create_profile_dedupes_existing_label(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    skeleton = profiles_dir / "_skeleton"
    skeleton.mkdir(parents=True)
    (profiles_dir / "dup").mkdir()
    monkeypatch.setattr(profile_mod, "PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(profile_mod, "SKELETON_DIR", skeleton)

    pid = profile_mod.create_profile("dup")
    assert pid == "dup-2"
