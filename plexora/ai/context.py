"""The Context Interpreter: a user's free-text note about a sample, normalised
by a cheap model into what an AI workflow can use.

"this is melnoma skn sample gate imune and tumor cells" becomes tissue skin,
disease melanoma, immune and tumour populations of interest -- and a scope of
every marker, because naming a population is not asking to gate only it. A
restriction ("only gate CD3, CD8 and SOX10") is the one thing that narrows a
run, and only when the words say so.

The model is the gateway's cheapest text class (`CAPABILITY`): one short call
with the note and the panel's names, a JSON schema, a small output budget,
no images and no history. It is not the gating agent and is never asked to
reason about the data.

What the model returns is not trusted as it stands (`settle`). Plexora checks
it against the user's own words and the panel, deterministically:

- **Scope.** `selected_markers` (or an exclusion) stands only when the note
  carries an explicit restriction cue (`RESTRICT`, `EXCLUDE`); otherwise it is
  demoted to every marker and the demotion is recorded as an ambiguity. Every
  named marker must resolve to one of the panel's (by name or canonical name);
  one that does not is an ambiguity, never a guess.
- **No invention.** A tissue, disease or species is kept only when each of
  its words is in the note, is the correction of a word in the note, or is a
  hedge ("possible"). "melanoma" does not become tissue skin.
- **The original.** The user's text is carried beside the interpretation,
  always; a failed call degrades to the original text alone, never to a
  narrower run.

Workflows bind it through `Interpretation`: gating turns it into the
session's `biology` and, for an explicit restriction, `markers`
(`decision.GatingWorkflow.context_arguments`). Any workflow that takes free
text about a sample (AutoQC next) calls `interpret` with its own unit noun.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from plexora.ai import vocabulary

#: The gateway's cheapest text class (licensing/src/ai/catalog.ts): the admin
#: routes it, the client never names a model.
CAPABILITY = "text_routine"
AGENT = "context_interpreter"
#: Enough for the JSON of a long note; small enough to stay negligible.
MAX_TOKENS = 900
MAX_TEXT = 1000
#: Characters of a marker's lineage shown to the model: enough to place it
#: in a group ("immune", "epithelial"), no more.
LINEAGE_CHARS = 48

#: Words that make a restriction explicit. "gate these markers" counts; a
#: marker or population merely named does not.
RESTRICT = re.compile(
    r"\b(only|just|solely|exclusively|restrict\w*|limit\w*|nothing\s+(?:else|but)|"
    r"except|these\s+markers?|the\s+following|no\s+other)\b", re.I)
EXCLUDE = re.compile(
    r"\b(skip\w*|exclud\w*|ignor\w*|except|without|leave\s+out|omit\w*|"
    r"don'?t\s+gate|do\s+not\s+gate|not\s+gate|no\s+need\s+to\s+gate)\b", re.I)
#: Hedges the model may add to keep the user's uncertainty ("possibly
#: melanoma" -> "possible melanoma"); they are not invented facts.
HEDGES = {"possible", "possibly", "probable", "probably", "likely", "suspected", "query",
          "presumed", "maybe", "uncertain", "unconfirmed", "sample", "tissue", "of", "the", "a"}
CONFIDENCE = ("high", "medium", "low")

SYSTEM = (
    "You normalise a scientist's short note about a tissue sample before an automated "
    "cell-gating run. You are not the gating agent: do not plan, reason about the data or "
    "add biology. Reply with one JSON object matching the schema, nothing else.\n"
    "- Fix obvious misspellings and expand clear abbreviations; write normalized_text as "
    "one or two plain sentences with the same meaning. List each fix in corrected_terms "
    "(as written -> corrected).\n"
    "- Extract tissue, disease and species ONLY when the note states them. Never infer one "
    "from another (melanoma does not imply skin). Keep hedges in the value: 'possibly "
    "melanoma' -> disease 'possible melanoma'.\n"
    "- markers_mentioned: markers the note names, spelled as in PANEL when the match is "
    "clear. populations_of_interest: cell populations it names.\n"
    "- gating_scope.mode is 'selected_markers' ONLY when the note explicitly restricts what "
    "is gated ('only gate', 'just', 'restrict to', 'skip everything except', 'gate these "
    "markers'). Naming markers or populations is NOT a restriction: the default is "
    "'all_markers'. requested_markers: PANEL names; for a group ('only the immune "
    "markers') list the PANEL markers whose lineage fits. excluded_markers: markers the "
    "note explicitly says not to gate.\n"
    "- Anything unclear goes in ambiguities; do not guess.")

SCHEMA = {
    "type": "object",
    "properties": {
        "normalized_text": {"type": "string"},
        "context": {"type": "object", "properties": {
            "tissue": {"type": ["string", "null"]},
            "disease": {"type": ["string", "null"]},
            "species": {"type": ["string", "null"]},
            "populations_of_interest": {"type": "array", "items": {"type": "string"}},
        }},
        "markers_mentioned": {"type": "array", "items": {"type": "string"}},
        "gating_scope": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["all_markers", "selected_markers"]},
            "requested_markers": {"type": "array", "items": {"type": "string"}},
            "excluded_markers": {"type": "array", "items": {"type": "string"}},
        }, "required": ["mode"]},
        "corrected_terms": {"type": "object", "additionalProperties": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
    },
    "required": ["normalized_text", "context", "gating_scope", "confidence"],
}


class ContextRefused(Exception):
    """The note cannot be honoured as asked (a restriction to markers the
    panel does not have): the run stops before anything is spent on it."""


@dataclass
class Term:
    """One unit the note may name: a marker (or channel) of the panel, with
    its canonical name and a short lineage for group requests."""
    name: str
    canonical: str | None = None
    lineage: str | None = None


@dataclass
class Interpretation:
    original_text: str
    normalized_text: str | None = None
    tissue: str | None = None
    disease: str | None = None
    species: str | None = None
    populations_of_interest: list = field(default_factory=list)
    markers_mentioned: list = field(default_factory=list)
    #: `all_markers` or `selected_markers`; `excluded` narrows all_markers.
    scope: str = "all_markers"
    requested: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    corrected_terms: dict = field(default_factory=dict)
    ambiguities: list = field(default_factory=list)
    confidence: str = "low"
    #: `model` (interpreted) or `unprocessed` (the call failed: the original
    #: text is all the workflow gets).
    source: str = "model"

    def as_dict(self) -> dict:
        return {"original_text": self.original_text, "normalized_text": self.normalized_text,
                "context": {k: v for k, v in (("tissue", self.tissue), ("disease", self.disease),
                                              ("species", self.species),
                                              ("populations_of_interest",
                                               self.populations_of_interest)) if v},
                "markers_mentioned_for_context": list(self.markers_mentioned),
                "gating_scope": {"mode": self.scope, "requested_markers": list(self.requested),
                                 **({"excluded_markers": list(self.excluded)}
                                    if self.excluded else {})},
                "corrected_terms": dict(self.corrected_terms),
                "ambiguities": list(self.ambiguities), "confidence": self.confidence,
                "source": self.source}

    def notes(self, limit: int = 400) -> str:
        """The interpretation as one short line for a workflow's free-text
        field: the normalised sentence, then what is not a tissue or disease."""
        parts = [self.normalized_text or self.original_text]
        if self.species:
            parts.append(f"Species: {self.species}.")
        if self.markers_mentioned:
            parts.append("Markers named for context: " + ", ".join(self.markers_mentioned) + ".")
        if self.ambiguities:
            parts.append("Unclear: " + "; ".join(self.ambiguities) + ".")
        return " ".join(p.strip() for p in parts if p and p.strip())[:limit]


def clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_TEXT]


def prompt(text: str, terms: list[Term], unit_noun: str = "marker") -> str:
    """The user turn: the panel (name, canonical name when it differs, short
    lineage) and the note."""
    rows = []
    for t in terms:
        row = t.name
        if t.canonical and vocabulary.fold(t.canonical) != vocabulary.fold(t.name):
            row += f" ({t.canonical})"
        if t.lineage:
            row += f": {t.lineage[:LINEAGE_CHARS]}"
        rows.append(row)
    return (f"PANEL ({unit_noun}s in this run)\n" + "\n".join(rows)
            + f"\n\nNOTE\n{text}")


def _words(text) -> list:
    return [w for w in re.findall(r"[0-9a-z]+", str(text).casefold()) if w]


def _stated(value, text, corrected) -> bool:
    """Every word of `value` is in the note, is what a word of the note was
    corrected to, or is a hedge."""
    said = set(_words(text))
    for wrong, right in corrected.items():
        if set(_words(wrong)) & said or vocabulary.fold(wrong) in vocabulary.fold(text):
            said |= set(_words(right))
    return all(w in said or w in HEDGES or _near(w, said) for w in _words(value))


def _near(word, said) -> bool:
    """A correction the model made but did not list: one edit away ('skn'
    -> 'skin'), for words long enough that this is not a coincidence."""
    if len(word) < 4:
        return False
    for other in said:
        if abs(len(other) - len(word)) <= 1 and _edits(word, other) <= 1 + (len(word) >= 8):
            return True
    return False


def _edits(a, b) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _resolver(terms: list[Term]):
    index = {}
    for t in terms:
        for key in (t.name, t.canonical):
            if key:
                index.setdefault(vocabulary.fold(key), t.name)

    def resolve(name):
        return index.get(vocabulary.fold(name))
    return resolve


def _names(values) -> list:
    return [clean(v)[:60] for v in (values or []) if isinstance(v, str) and clean(v)]


def settle(raw, text: str, terms: list[Term], unit_noun: str = "marker") -> Interpretation:
    """The model's answer checked against the note and the panel (see the
    module docstring). Pure: the same answer always settles the same way."""
    raw = raw if isinstance(raw, dict) else {}
    ctx = raw.get("context") if isinstance(raw.get("context"), dict) else {}
    scope = raw.get("gating_scope") if isinstance(raw.get("gating_scope"), dict) else {}
    corrected = {clean(k)[:60]: clean(v)[:60] for k, v in (raw.get("corrected_terms") or {}).items()
                 if isinstance(k, str) and isinstance(v, str) and clean(k) and clean(v)} \
        if isinstance(raw.get("corrected_terms"), dict) else {}
    ambiguities = _names(raw.get("ambiguities"))
    out = Interpretation(original_text=text, corrected_terms=corrected,
                         normalized_text=clean(raw.get("normalized_text"))[:400] or None,
                         confidence=raw.get("confidence") if raw.get("confidence") in CONFIDENCE
                         else "low")
    for key in ("tissue", "disease", "species"):
        value = clean(ctx.get(key))[:120] if isinstance(ctx.get(key), str) else ""
        if not value or value.casefold() in ("null", "none", "unknown", "n/a"):
            continue
        if _stated(value, text, corrected):
            setattr(out, key, value)
        else:
            ambiguities.append(f"{key} '{value}' was not stated in the note, so it is not used")
    out.populations_of_interest = _names(ctx.get("populations_of_interest"))[:12]

    resolve = _resolver(terms)

    def panel_names(values, what):
        found = []
        for name in _names(values):
            hit = resolve(name)
            if hit is None:
                ambiguities.append(f"'{name}' ({what}) is not a {unit_noun} of this panel")
            elif hit not in found:
                found.append(hit)
        return found

    out.markers_mentioned = panel_names(raw.get("markers_mentioned"), "mentioned")[:24]
    requested = panel_names(scope.get("requested_markers"), "requested")
    excluded = panel_names(scope.get("excluded_markers"), "excluded")
    if scope.get("mode") == "selected_markers":
        if not RESTRICT.search(text):
            ambiguities.append("the note names markers or populations but does not restrict "
                               "the run to them, so every marker is gated")
        elif requested:
            out.scope, out.requested = "selected_markers", requested
        else:
            # Asked to restrict, and none of it is in the panel: never fall
            # back to a narrower or a wider run silently.
            out.scope = "selected_markers"
            ambiguities.append("the note restricts the run, but none of the markers it asks "
                               "for is in this panel")
    if excluded and out.scope == "all_markers":
        if EXCLUDE.search(text):
            out.excluded = excluded
        else:
            ambiguities.append("markers were listed to skip without the note saying so; "
                               "none is skipped")
    out.ambiguities = list(dict.fromkeys(a[:200] for a in ambiguities))[:8]
    return out


def unprocessed(text: str, why: str) -> Interpretation:
    """When the interpreter cannot run: the note as written, every marker."""
    return Interpretation(original_text=text, source="unprocessed", confidence="low",
                          ambiguities=[f"the note could not be interpreted ({why}); it is "
                                       "passed on as written"])


def interpret(text, terms: list[Term], *, gateway, feature: str, idempotency_key: str,
              unit_noun: str = "marker", session_id: str | None = None):
    """(Interpretation, response or None). Never raises for the model's sake:
    a refused or unreadable call gives `unprocessed`. Empty text is not sent."""
    from plexora.ai.harness.gateway import GatewayError
    from plexora.ai.harness.wire import ModelRequest, text_block

    text = clean(text)
    if not text:
        return None, None
    request = ModelRequest(
        capability=CAPABILITY, system=[text_block(SYSTEM)],
        messages=[{"role": "user", "content": [text_block(prompt(text, terms, unit_noun))]}],
        max_tokens=MAX_TOKENS, output_schema=SCHEMA,
        context={"feature": feature, "agent": AGENT, "workflow": "context",
                 **({"session_id": session_id} if session_id else {})})
    try:
        response = gateway.messages(request, idempotency_key=idempotency_key)
    except GatewayError as exc:
        return unprocessed(text, exc.code), None
    try:
        raw = response.json()
    except ValueError:
        return unprocessed(text, "the reply was not JSON"), response
    return settle(raw, text, terms, unit_noun), response
