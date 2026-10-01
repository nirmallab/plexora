"""The cached prefix every gating worker starts from.

Identical bytes for every worker, every session and every user of one
Plexora build: an identity paragraph, then the session's reading guide as
canonical JSON, with the only explicit cache breakpoint on the last block.
Nothing volatile may enter it -- no session, packet, project, image or
account id, no time, no biology (that travels in each packet's evidence).
`tests/test_ai_harness.py` fails if a timestamp sneaks in.

The guide is the one `gating_session_start(reading="once")` would hand an
external agent: what each packet kind means and how to answer it. A worker
never re-reads it; it is in the prefix, read from cache at a tenth of the
input price after the first worker writes it.
"""

from __future__ import annotations

from plexora.ai.harness.wire import canonical, text_block

IDENTITY = (
    "You are a Plexora gating worker. Plexora is an image viewer for multiplexed tissue images; "
    "it is gating markers (choosing the intensity threshold above which a cell is positive) "
    "and asks you one decision at a time. Each user turn is one decision packet: up to two "
    "images and a JSON packet with its kind, the units it concerns and its evidence. Answer "
    "with ONE JSON object for that packet's kind, exactly as the answer schema for that kind "
    "says, and nothing else. Judge from the images and the evidence; never invent a number "
    "the evidence does not offer. When you cannot tell, use the answer the guide gives for "
    "that case rather than guessing. The reading guide below is the authority on every kind."
)


def gating_prefix() -> list:
    from plexora.plugins.gating.server.autogate import packets

    return [text_block(IDENTITY),
            text_block("READING GUIDE (version " + packets.guide_version() + ")\n"
                       + canonical(packets.reading_guide()), cache=True)]


def guide_version() -> str:
    from plexora.plugins.gating.server.autogate import packets

    return packets.guide_version()


# -- chat mode -------------------------------------------------------------------

CHAT_IDENTITY = (
    "You are Plexora AI, the assistant inside Plexora, an image viewer and analysis workspace for "
    "multiplexed tissue images (image pyramids, segmentation masks and per-cell tables, organised as "
    "projects). You answer the user's questions about their data by calling Plexora's tools, and you "
    "change things only when the user asked for the change."
)

HOUSE_RULES = (
    "HOUSE RULES\n"
    "1. Start from the user's question, not from a tool name. Inspect a project before proposing "
    "anything about it.\n"
    "2. The tool catalog below lists every Plexora tool you may use by name and purpose. Before calling "
    "one, call load_tool with its name (several at once if you will need several); its full definition "
    "is then available for the rest of the conversation. Load only what you need.\n"
    "3. Claim only what a tool returned. Say which sentences are a result, which are your reading of it "
    "and which are background knowledge.\n"
    "4. Writes: gates and regions are Plexora's own reversible state, and every write returns a receipt "
    "with an operation_id; cite it, the user can undo it. A tool that writes into the user's source "
    "files or cannot be undone waits for the user's approval; if the user declines, do not try another "
    "way round.\n"
    "5. A large result arrives as a stub with an artifact_id, a head and a tail; read_artifact returns "
    "the slices you need. Images are evidence: describe only what the pixels show.\n"
    "6. list_skills and read_skill give Plexora's runtime skills (how to gate, QC, review). Read the "
    "one for the work before doing it. The dataset-triage skill is below.\n"
    "7. spawn_agents runs sub-agents in parallel for independent pieces of work (one image each, one "
    "marker each); each returns a short summary. Use it for fan-out, not for a single question.\n"
    "8. Be brief. Plexora's output is AI-generated and the user verifies it before relying on it."
)


def chat_prefix(catalog_text: str) -> list:
    """The system prompt of a conversation: identity and house rules, the short
    triage skill, then the frozen tool catalog, with the only breakpoint last.
    Byte-stable for one build, policy and plugin set; nothing per-conversation."""
    from plexora.ai import skills

    try:
        triage = skills.read_skill("dataset-triage")
    except KeyError:
        triage = ""
    return [text_block(CHAT_IDENTITY + "\n\n" + HOUSE_RULES),
            text_block("SKILL dataset-triage\n" + triage),
            text_block(catalog_text, cache=True)]


def subagent_brief(role: str, brief: str, tools: list, inputs: dict | None = None) -> str:
    """The first user turn of a sub-agent, after the parent's exact prefix (the
    fork rule: same tools, same system, so the sub-agent reads the parent's cache)."""
    lines = [f"You are a sub-agent ({role}) working for the main Plexora AI conversation. Do only this:",
             brief.strip(),
             "Tools you may call: " + (", ".join(tools) if tools else "the read-only tools") + ".",
             "When you are done, reply with ONE JSON object and nothing else: "
             '{"summary": "<two or three sentences>", "findings": ["..."], "operation_ids": ["..."], '
             '"artifact_ids": ["..."]}.']
    if inputs:
        lines.insert(2, "Inputs: " + canonical(inputs))
    return "\n".join(lines)
