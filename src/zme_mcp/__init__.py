"""zme-mcp — Zeug Memory Engine as an MCP server."""


def main() -> None:
    # Imported on call so `zme_mcp.workspace` works without the server's dependencies.
    from .server import main as run
    run()


if __name__ == "__main__":
    main()
