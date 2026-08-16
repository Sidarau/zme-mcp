"""zme-mcp — Zeug Memory Engine as an MCP server."""


def main() -> None:
    # Keep backend utilities importable for diagnostics/tests before optional
    # server dependencies are installed.
    from .server import main as server_main

    server_main()

if __name__ == "__main__":
    main()
