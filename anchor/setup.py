"""anchor-setup — detect installed agent clients and wire anchor into their
MCP config. Backs up every config it touches. Idempotent (safe to re-run).

    anchor-setup                 # detect + offer each found client
    anchor-setup --yes           # configure every detected client, no prompts
    anchor-setup opencode claude-code codex   # only these
"""
import json
import os
import re
import shutil
import sys

HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))


def anchor_command():
    """The command clients should run: the console script if on PATH, else
    this interpreter running the module (works for folder-copy installs)."""
    if shutil.which("anchor-mcp"):
        return ["anchor-mcp"]
    return [sys.executable, "-m", "anchor.mcp_server"]
def jsonc_loads(text):
    """JSON with comments / trailing commas (OpenCode configs allow both)."""
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)


def jmerge_servers(cfg, key, entry):
    cfg.setdefault(key, {})
    already = "anchor" in cfg[key]
    cfg[key]["anchor"] = entry
    return already


def codex_merge(text):
    already = "[mcp_servers.anchor]" in text
    if not already:
        text = text.rstrip("\n") + ("\n" if text.strip() else "")
        text += ('\n[mcp_servers.anchor]\ncommand = "anchor-mcp"\n')
    return text, already


CLIENTS = {
    "opencode": {
        "config": os.path.join(HOME, ".config", "opencode", "opencode.json"),
        "load": jsonc_loads,
        "merge": lambda cfg: jmerge_servers(
            cfg, "mcp", {"type": "local", "command": anchor_command()}),
        "label": "OpenCode",
    },
    "claude-code": {
        "config": os.path.join(HOME, ".claude.json"),
        "load": json.loads,
        "merge": lambda cfg: jmerge_servers(cfg, "mcpServers", {"command": "anchor-mcp"}),
        "label": "Claude Code",
    },
    "claude-desktop": {
        "config": os.path.join(APPDATA, "Claude", "claude_desktop_config.json"),
        "load": json.loads,
        "merge": lambda cfg: jmerge_servers(cfg, "mcpServers", {"command": "anchor-mcp"}),
        "label": "Claude Desktop",
    },
    "cursor": {
        "config": os.path.join(HOME, ".cursor", "mcp.json"),
        "load": json.loads,
        "merge": lambda cfg: jmerge_servers(cfg, "mcpServers", {"command": "anchor-mcp"}),
        "label": "Cursor",
    },
    "windsurf": {
        "config": os.path.join(HOME, ".codeium", "windsurf", "mcp_config.json"),
        "load": json.loads,
        "merge": lambda cfg: jmerge_servers(cfg, "mcpServers", {"command": "anchor-mcp"}),
        "label": "Windsurf",
    },
    "codex": {
        "config": os.path.join(HOME, ".codex", "config.toml"),
        "load": None,  # TOML handled textually
        "merge": codex_merge,
        "label": "Codex CLI",
    },
}


def configure(name):
    spec = CLIENTS[name]
    path = spec["config"]
    label = spec["label"]
    if not os.path.exists(path):
        return f"  {label}: config not found ({path}) — skipped"
    backup = path + ".anchor-backup"
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        with open(backup, "w", encoding="utf-8") as f:
            f.write(raw)  # one-keystroke undo
    except OSError as e:
        return f"  {label}: could not read/backup config — {e}"

    try:
        if name == "codex":
            new_raw, already = codex_merge(raw)
            if already:
                return f"  {label}: already configured ✓"
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_raw)
            return f"  {label}: configured ✓ (backup at {backup})"
        cfg = spec["load"](raw) if raw.strip() else {}
        already = spec["merge"](cfg)
        if already:
            return f"  {label}: already configured ✓"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return f"  {label}: configured ✓ (backup at {backup})"
    except Exception as e:
        return f"  {label}: FAILED — {e}; config restored from {backup}"


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = list(sys.argv[1:] if argv is None else argv)
    yes = "--yes" in argv
    argv = [a for a in argv if not a.startswith("--")]
    print("anchor-setup — wire anchor into your agent tools\n")

    # environment check
    try:
        import anchor
        print(f"anchor {anchor.__version__} — command: {' '.join(anchor_command())}")
    except ImportError:
        print("ERROR: anchor is not importable — run pip install anchor-memory first")
        return 1

    from .replay import DEFAULT_DB
    print(f"OpenCode database: {'found' if os.path.exists(DEFAULT_DB) else 'not found (manual memories via anchor_remember still work)'}"
          f" — {DEFAULT_DB}\n")

    if argv:
        targets = [a for a in argv if a in CLIENTS]
        unknown = [a for a in argv if a not in CLIENTS]
        if unknown:
            print(f"unknown clients: {', '.join(unknown)}")
            print(f"known: {', '.join(CLIENTS)}")
            return 1
    else:
        targets = [n for n, s in CLIENTS.items() if os.path.exists(s["config"])]

    if not targets:
        print("no supported agent clients detected.")
        print("supported: " + ", ".join(f"{k} ({CLIENTS[k]['label']})" for k in CLIENTS))
        print("install a client first, or pass a name: anchor-setup opencode")
        return 1

    print("clients detected:")
    for name, spec in CLIENTS.items():
        marker = "x" if name in targets else " "
        print(f"  [{marker}] {name:<14} {CLIENTS[name]['config']}")

    for name in targets:
        if not yes:
            ans = input(f"\nconfigure {CLIENTS[name]['label']}? [Y/n] ").strip().lower()
            if ans in ("n", "no"):
                print(f"  {CLIENTS[name]['label']}: skipped")
                continue
        print(configure(name))

    print("\ndone. restart your agent tool(s) to activate — anchor's 8 memory"
          " tools will be available next session.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
