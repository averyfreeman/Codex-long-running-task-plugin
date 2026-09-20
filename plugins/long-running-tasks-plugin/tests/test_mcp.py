import asyncio

from long_running_tasks.mcp_server import TOOL_DEFINITIONS, list_tools


def test_tool_schemas_are_discoverable_and_bounded():
    names = {tool.name for tool in TOOL_DEFINITIONS}
    assert names == {"start_run", "get_run_status", "list_runs", "get_run_logs", "resume_run", "cancel_run", "recover_run"}
    for tool in TOOL_DEFINITIONS:
        assert len(tool.description) >= 30
        assert "Use" in tool.description
        assert tool.inputSchema["type"] == "object"
        for definition in tool.inputSchema.get("properties", {}).values():
            assert definition.get("description")
    listed = asyncio.run(list_tools())
    assert {tool.name for tool in listed} == names


def test_tool_annotations_distinguish_reads_and_mutations():
    tools = {tool.name: tool for tool in TOOL_DEFINITIONS}
    for name in {"get_run_status", "list_runs", "get_run_logs"}:
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    for name in {"start_run", "resume_run", "recover_run"}:
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False
    assert tools["cancel_run"].annotations.readOnlyHint is False
    assert tools["cancel_run"].annotations.destructiveHint is True
