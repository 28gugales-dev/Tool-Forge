# /toolforge-bridge

Manage the ToolForge REST API + webhook bridge server (port 7842). The bridge exposes your ToolForge state to external agents and tools like Hermes and Obsidian.

## Usage

```
/toolforge-bridge start
/toolforge-bridge status
/toolforge-bridge hermes-status
/toolforge-bridge obsidian-status
/toolforge-bridge export
```

## Sub-commands

| Command | What it does |
|---------|--------------|
| `start` | Launch the bridge server on port 7842 (background) |
| `status` | Show recent context_sync events |
| `hermes-status` | Show Hermes sync history + reachability check |
| `obsidian-status` | Show Obsidian sync history + reachability check |
| `export` | Print the full context bundle (profile + stacks + shortcuts) |

## Bridge API Endpoints

Once running at `http://127.0.0.1:7842`:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Liveness + DB schema version |
| GET | `/api/profile` | Full user preference profile |
| GET | `/api/skills` | Installed skills by usage |
| GET | `/api/stacks` | All skill stacks |
| GET | `/api/pipelines` | Recent pipelines |
| GET | `/api/shortcuts` | Workflow shortcuts |
| GET | `/api/export/context` | Full context bundle |
| POST | `/api/context/ingest` | Receive external context |
| POST | `/api/webhooks/hermes` | Hermes pushes context here |
| POST | `/api/webhooks/obsidian` | Obsidian pushes notes here |

## Configuration

Add to `~/.claude/toolforge-config.json`:

```json
{
  "hermes_base_url": "http://localhost:8000",
  "hermes_api_key": "optional-key",
  "obsidian_base_url": "https://127.0.0.1:27123",
  "obsidian_api_key": "your-local-rest-api-key",
  "obsidian_vault_folder": "ToolForge Sessions",
  "learner_push_hermes": true,
  "learner_push_obsidian": true
}
```

---

```bash
# Start bridge server
python webui/bridge_server.py --port 7842

# Check context export
curl http://127.0.0.1:7842/api/export/context

# View sync history
python bin/toolforge_db.py sync_history
```
