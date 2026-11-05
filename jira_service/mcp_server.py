"""Installed entry point for the same bridge shipped in the portable plugin."""
def main():
    from .mcp_bridge import main as run
    run()
