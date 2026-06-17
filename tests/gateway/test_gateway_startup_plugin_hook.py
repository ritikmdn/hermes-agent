from __future__ import annotations

from gateway import run as gateway_run


def test_gateway_startup_plugin_hook_invokes_python_plugins(monkeypatch):
    gateway = object()
    calls: list[tuple[str, object]] = []

    def fake_invoke_hook(name: str, **kwargs):
        calls.append((name, kwargs["gateway"]))
        return [{"action": "started"}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", fake_invoke_hook)

    result = gateway_run._invoke_gateway_startup_plugin_hooks(gateway)

    assert result == [{"action": "started"}]
    assert calls == [("gateway_startup", gateway)]
