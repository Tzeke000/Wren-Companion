# Ava Tool System

Tools are modular capabilities Ava can invoke with `[TOOL:<name> {...}]`.

## Structure

- `tool_registry.py`: central runtime registry
- `system/`: local machine and process helpers
- `web/`: internet lookup/fetch helpers
- `ava/`: self-memory and self-note helpers

## How tools are registered

Each tool module calls `register_tool(...)` at import time.
The `ToolRegistry` imports all modules in `load_builtin_tools()` so tools self-register automatically.

## Adding a new tool

1. Add a function with signature:
   - `fn(params: dict, g: dict) -> ToolResult`
2. Register it with:
   - `register_tool(name, description, tier, fn)`
3. Ensure the module is imported by `load_builtin_tools()`.

## Tier meaning

- `1`: autonomous local tool use
- `2`: requires verbal check-in behavior
- `3`: requires explicit confirmation

