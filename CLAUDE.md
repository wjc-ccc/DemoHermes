# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DemoCursor is an autonomous Agent framework that reproduces Cursor's agent capabilities (via `claude-agent-sdk`) with two additions: full-step observability/reproducibility and self-evolution (memory curation + automatic Skill creation). The project is in early development — most modules are docstring skeletons; only config (`Provider.py`), logging (`logging/`), type definitions (`core/types.py`), and the `BaseModel` ABC have real implementations.

## Current State: Package Rename In Progress

The codebase is mid-refactor from `Agent/` (capital A) to `agent/` (lowercase). `git status` shows `Agent/**` deleted and `agent/**` added. Expect these consequences:
- **Imports are inconsistent and many are broken.** You will find `from Agent.Provider import ...` (old, in docstrings), `from baseStructure import BaseModel` (should be `from .baseStructure import BaseModel`), `from core.types import ...` (should be relative), and `from types import Session` (shadows the stdlib `types` module). The canonical package is **`agent`** (lowercase); run modules as `python -m agent.gateway.cli`, not `python -m Agent...`.
- Some READMEs still reference old `Agent.` paths.
- `docs/aiDocs/7.13 简单Loop实现.md` catalogs the specific bugs blocking the minimal loop and gives a corrected reference implementation of `types.py`, `context_builder.py`, `loop.py`, `deepseek.py`, `cli.py`. **Treat it as the source of truth for the intended design** when wiring up the loop.

## Architecture (big picture)

The agent is built around a four-phase loop, with every capability plugged into it:

```
gateway (cli / api / channel)
   │
   ▼
core/loop  ── observe → think → act → persist
                │         │       │        │
                │         │       │        └→ memory (L1/L2/L3) + trajectory
                │         │       └→ tools (dispatch tool_call, re-inject result, re-think until no calls)
                │         └→ model (LLM inference, decides tool use)
                └→ prompt/ContextBuilder (system + hot memory + retrieved episodic + skills + messages)
   │
   └→ evolution (background: reviewer, memory_curator, skill_creator/optimizer, scheduler)
```

Module roles (each directory has a `README_*.md` with detail):
- **`core/`** — `loop.py` (dispatcher), `session.py` (lifecycle), `types.py` (`Message`/`Session`/`ToolCall`/`MemoryItem` — pydantic structures everything depends on), `context_builder.py` (Session → OpenAI-format `messages`), `events.py` (event model for visualization/replay).
- **`model/`** — `baseStructure.py` (`BaseModel` ABC: `chat(messages) -> str`), providers (`deepseek.py` OpenAI-compatible, `claude.py` via claude-agent-sdk), `registry.py` (name → provider). Upper layers call the registry and never touch provider specifics.
- **`tools/`** — `base.py` (Tool ABC: `name`/`description`/`schema`/`execute`), `registry.py` (collect schemas for prompt injection + dispatch calls), built-ins: `file_tools`, `bash_tools`, `memory_tools`.
- **`memory/`** — three layers, **do not build all at once** (per dev plan): L1 `hot/` (Markdown `data/memories/USER.md` + `MEMORY.md`, always injected into the system prompt), L2 `episodic/` (SQLite + FTS), L3 `procedural/` (Skills, progressive load `name→params→body`). `manager.py` is the only entry point the rest of the code should use.
- **`prompt/`** — `builder.py` (layered system prompt), `snapshot.py` (freeze USER.md/MEMORY.md at session start so a session's prompt stays stable), `systemPrompt.md`/`evolutionPrompt.md` templates.
- **`evolution/`** — `scheduler.py` orchestrates: `reviewer` (sync, after each turn), `memory_curator` (async, compress MEMORY.md), `skill_creator`/`skill_optimizer` (background). Must not block the main loop.
- **`gateway/`** — `cli.py` (terminal, dev entry), `api.py` (HTTP + WebSocket for the visualization frontend).
- **`channel/`** — external message adapters (e.g. `wechat.py`); parses inbound messages into `core.types.Message`.

## Configuration

Config is split across two files, loaded by two different mechanisms — don't conflate them:
- **`.env`** (gitignored) — API keys and `MODEL` selection. Loaded by `agent/Provider.py` via `python-dotenv`. Import values from there: `from agent.Provider import DEEPSEEK_API_KEY, MODEL`. Do **not** call `load_dotenv()` elsewhere. `.env.example` is the template (the real `.env` points DeepSeek/Claude at Volcengine Ark URLs, not the `api.deepseek.com` shown in the example).
- **`config.yml`** — logging only (`LOG_PATH`, `LOG_LEVEL`). Parsed manually by `logging/loggingTool.py`; the `yml` package listed in requirements is not actually used.

`MODEL` selects the active provider (`deepseek-v4-flash`, `glm-5.2`, `minimax-2.7`). DeepSeek is accessed through an OpenAI-compatible client (`openai` SDK), not a native SDK.

## Logging

`logging/` is a wrapper around the stdlib `logging` module. Because the package itself is named `logging`, `logging/__init__.py` loads the real stdlib `logging` from its filesystem path to avoid a name clash. Entry points call `setup_logging()` once; everything else uses `import logging; logging.getLogger(__name__)`. Config comes from `config.yml`; output goes to `temp/runtime.log` plus stdout.

## Commands

```bash
pip install -r requirements.txt   # NOTE: requirements.txt is incomplete — pydantic and python-dotenv are used but not listed

# Intended entry points (loop/cli/api are stubs as of 7.13 — see the reference impl doc to wire them up):
python -m agent.gateway.cli       # terminal chat
python -m agent.gateway.api       # HTTP + WebSocket server

# Working self-tests:
python -m logging.loggingTool     # verify logging config + file output
python -m agent.model.deepseek    # DeepSeek connectivity (after fixing the import bugs in deepseek.py)
```

There is no test framework configured (no pytest). `test/` is gitignored and used for ad-hoc scripts.

## Conventions

- Every directory has a `README_*.md` (some prefixed `A ` to sort first) describing that module's responsibility and file roles — read it before working in a module.
- Development is tracked by date in `docs/timeDocs/` (e.g. `7.13进度.md`), architecture decisions in `docs/aiDocs/`, debug logs in `docs/errorDocs/`. The staged plan in `docs/aiDocs/7.12 架构.md` defines 6 phases (foundation → chat → tools → memory → observability → Claude SDK → evolution) and dictates build order: **loop and memory L1 first, evolution last.** Don't jump ahead (e.g. evolution before the loop has data to process).
- `data/` and `temp/` are gitignored runtime data; `data/memories/*.md` are live (Agent-maintained) and `data/skills/` is empty until `skill_creator` runs.
- Comments and docs are bilingual: Chinese for design intent, English for identifiers. Match this when editing existing files.
