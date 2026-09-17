from core.wechat_instance import resolve_wechat_instance_settings


def test_sibling_checkouts_get_isolated_defaults(tmp_path):
    first = resolve_wechat_instance_settings(tmp_path / "stable", {})
    second = resolve_wechat_instance_settings(tmp_path / "v2", {})

    assert first.instance_id != second.instance_id
    assert first.data_root != second.data_root
    assert first.port_base != second.port_base
    assert set(first.ports).isdisjoint(second.ports)


def test_explicit_instance_settings_are_stable_and_passed_to_child(tmp_path):
    env = {
        "OPENNEXUS_WECHAT_INSTANCE_ID": "stable-server",
        "OPENNEXUS_WECHAT_DATA_DIR": str(tmp_path / "wechat"),
        "OPENNEXUS_WECHAT_PORT_BASE": "34100",
        "OPENNEXUS_WECHAT_PORT_COUNT": "4",
        "OPENNEXUS_INTERNAL_URL": "http://127.0.0.1:8123/",
    }
    settings = resolve_wechat_instance_settings(tmp_path / "checkout", env)
    child = settings.child_env(env, api_token="secret")

    assert settings.instance_id == "stable-server"
    assert settings.ports == (34100, 34101, 34102, 34103)
    assert child["WCC_DATA_DIR"] == str(tmp_path / "wechat")
    assert child["OPENNEXUS_WECHAT_INSTANCE_ID"] == "stable-server"
    assert child["WCC_API_URL"] == "http://127.0.0.1:8123"
    assert child["WCC_API_TOKEN"] == "secret"


def test_child_env_uses_runtime_internal_url_override(tmp_path):
    settings = resolve_wechat_instance_settings(tmp_path / "checkout", {})
    child = settings.child_env({
        "OPENNEXUS_INTERNAL_URL": "http://127.0.0.1:9000",
    })

    assert child["WCC_API_URL"] == "http://127.0.0.1:9000"
