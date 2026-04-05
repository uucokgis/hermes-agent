from pathlib import Path

from agent.auto_workflows import (
    build_meridian_workflow_overlay,
    should_auto_route_meridian_message,
)


def test_should_auto_route_meridian_message_matches_direct_work_requests():
    assert should_auto_route_meridian_message("Meridian icin su feature'i yap") is True
    assert should_auto_route_meridian_message("Meridian'de bunu Philip ve Fatih ile hallet") is True
    assert should_auto_route_meridian_message("Philip efendi backlog'taki tasklari ready'e cekmiyor") is True
    assert should_auto_route_meridian_message("hello there") is False
    assert should_auto_route_meridian_message("this branch is ready for review") is False
    assert should_auto_route_meridian_message("/skills list") is False
    assert should_auto_route_meridian_message("Meridian", delegate_depth=1) is False


def test_build_meridian_workflow_overlay_loads_skill(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    skill_dir = hermes_home / "skills" / "meridian" / "workflow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: meridian-workflow\n"
        "description: Meridian workflow skill\n"
        "---\n"
        "\n"
        "# Meridian Workflow\n"
        "Follow the workflow.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    overlay = build_meridian_workflow_overlay("Meridian icin bunu yap")

    assert overlay is not None
    assert "Meridian Workflow" in overlay
    assert "The user has provided the following instruction" in overlay
    assert "task_claim/task_transition" in overlay
