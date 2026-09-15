"""Provider-neutral structured LLM clients for Z.AI GLM, Groq, and Ollama."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import json
import logging
import os
from typing import Any, Protocol
from urllib import error, request

from jsonschema import ValidationError, validate


LOG = logging.getLogger("insurance_audit.llm")


class LLMError(RuntimeError):
    """Raised when a model server cannot return a valid structured response."""


class StructuredLLMClient(Protocol):
    provider: str
    model: str
    recommended_max_chunk_chars: int

    def generate(self, messages: list[dict[str, str]], response_schema: dict[str, Any]) -> dict[str, Any]:
        """Generate one JSON object constrained by ``response_schema``."""


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt our schema to Groq strict-mode requirements without mutating it."""
    result = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    return result


def recover_failed_generation(body: Any, response_schema: dict[str, Any]) -> dict[str, Any] | None:
    """Repair safe empty-container defects, then require full schema validity.

    Missing empty rule arrays carry no extracted facts. Source-coverage validation
    later rejects the fragment if the contract text actually required such a rule.
    """
    details = body.get("error", body) if isinstance(body, dict) else {}
    generated = details.get("failed_generation")
    if not isinstance(generated, str):
        return None
    try:
        candidate = json.loads(generated)
    except json.JSONDecodeError:
        return None
    if not isinstance(candidate, dict):
        return None
    top_properties = response_schema.get("properties", {})
    metadata_schema = top_properties.get("metadata")
    if metadata_schema is not None:
        metadata = candidate.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            return None
        for field, field_schema in metadata_schema.get("properties", {}).items():
            allowed_type = field_schema.get("type")
            if field not in metadata and isinstance(allowed_type, list) and "null" in allowed_type:
                metadata[field] = None
    if "services" in top_properties:
        candidate.setdefault("services", [])
    rules = candidate.setdefault("rules", {})
    if ("services" in top_properties and not isinstance(candidate.get("services"), list)) or not isinstance(rules, dict):
        return None
    for field in ("calculation_order", "warnings"):
        if field not in candidate and field in rules:
            candidate[field] = rules.pop(field)
    candidate.setdefault("calculation_order", [])
    candidate.setdefault("warnings", [])
    rule_properties = (
        response_schema.get("properties", {}).get("rules", {}).get("properties", {})
    )
    for field, field_schema in rule_properties.items():
        if field_schema.get("type") == "array":
            rules.setdefault(field, [])
    try:
        validate(instance=candidate, schema=strict_json_schema(response_schema))
    except ValidationError:
        return None
    return candidate


@dataclass
class GroqClient:
    """Groq Chat Completions client using strict JSON Schema output."""

    model: str = "openai/gpt-oss-20b"
    api_key: str | None = None
    temperature: float = 0.0
    # Keep the complete request below Groq's common on-demand TPM limit.  The
    # contract agent also uses the provider's chunk recommendation below.
    max_completion_tokens: int = 3072
    recommended_max_chunk_chars: int = 6000
    reasoning_effort: str = "medium"
    schema_attempts: int = 2
    provider: str = "groq"
    last_raw_response: str | None = None

    def generate(self, messages: list[dict[str, str]], response_schema: dict[str, Any]) -> dict[str, Any]:
        try:
            from groq import Groq
        except ImportError as exc:
            raise LLMError("Groq SDK is not installed; run: .venv/bin/pip install -e .") from exc
        key = self.api_key or os.getenv("GROQ_API_KEY")
        if not key:
            raise LLMError("GROQ_API_KEY is not set")
        self.last_raw_response = None
        client = Groq(api_key=key)
        current_messages = messages
        for attempt in range(1, self.schema_attempts + 1):
            try:
                completion = client.chat.completions.create(
                    model=self.model,
                    messages=current_messages,
                    temperature=self.temperature,
                    max_completion_tokens=self.max_completion_tokens,
                    reasoning_effort=self.reasoning_effort,
                    stream=False,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "contract_fragment",
                            "strict": True,
                            "schema": strict_json_schema(response_schema),
                        },
                    },
                )
                if hasattr(completion, "model_dump_json"):
                    self.last_raw_response = completion.model_dump_json(indent=2)
                else:
                    self.last_raw_response = json.dumps(completion, default=str, indent=2)
                content = completion.choices[0].message.content
                result = json.loads(content or "")
                break
            except Exception as exc:
                body = getattr(exc, "body", None)
                if body:
                    self.last_raw_response = json.dumps(body, default=str, indent=2)
                details = body.get("error", body) if isinstance(body, dict) else {}
                is_schema_failure = (
                    getattr(exc, "status_code", None) == 400
                    and (details.get("code") == "json_validate_failed"
                         or "json_validate_failed" in str(exc))
                )
                recovered = recover_failed_generation(body, response_schema) if is_schema_failure else None
                if recovered is not None:
                    LOG.warning("recovered Groq output with deterministic structural repair")
                    result = recovered
                    break
                if is_schema_failure and attempt < self.schema_attempts:
                    LOG.warning("Groq produced invalid schema output; retrying this chunk (%d/%d)",
                                attempt + 1, self.schema_attempts)
                    correction = (
                            "Your previous generation was invalid JSON. Return exactly one object matching "
                            "the supplied schema. Do not insert empty strings into arrays. Keep "
                            "calculation_order and warnings at the top level, outside rules."
                    )
                    current_messages = [
                        {**messages[0], "content": messages[0]["content"] + "\n\n" + correction},
                        *messages[1:],
                    ]
                    continue
                code = details.get("code")
                message = details.get("message") or str(exc)
                if isinstance(message, str):
                    message = message.split(" See 'failed_generation'", 1)[0]
                raise LLMError(f"Groq request failed ({code or 'api_error'}): {message}") from exc
        if not isinstance(result, dict):
            raise LLMError("Groq structured response must be a JSON object")
        return result


@dataclass
class GLMClient:
    """Z.AI GLM client using JSON mode plus local strict-schema validation."""

    model: str = "glm-5.3"
    api_key: str | None = None
    temperature: float = 0.0
    max_completion_tokens: int = 8192
    # GLM-5.3 always reasons and accepts low/high/max rather than enabled/disabled.
    reasoning_effort: str = "low"
    schema_attempts: int = 2
    recommended_max_chunk_chars: int = 12_000
    provider: str = "glm"
    last_raw_response: str | None = None

    def generate(self, messages: list[dict[str, str]], response_schema: dict[str, Any]) -> dict[str, Any]:
        try:
            from zai import ZaiClient
        except ImportError as exc:
            raise LLMError("Z.AI SDK is not installed; run: .venv/bin/pip install -e .") from exc
        key = self.api_key or os.getenv("GLM_API_KEY")
        if not key:
            raise LLMError("GLM_API_KEY is not set")

        schema = strict_json_schema(response_schema)
        schema_instruction = (
            "Return exactly one JSON object and no surrounding prose or Markdown. "
            "The object must validate against this JSON Schema:\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        current_messages = [dict(message) for message in messages]
        if current_messages and current_messages[0].get("role") == "system":
            current_messages[0]["content"] += "\n\n" + schema_instruction
        else:
            current_messages.insert(0, {"role": "system", "content": schema_instruction})

        self.last_raw_response = None
        client = ZaiClient(api_key=key)
        last_error = "unknown structured-output error"
        for attempt in range(1, self.schema_attempts + 1):
            try:
                completion = client.chat.completions.create(
                    model=self.model,
                    messages=current_messages,
                    temperature=self.temperature,
                    max_tokens=self.max_completion_tokens,
                    # GLM-5.3 always reasons. ``thinking`` enables that path;
                    # ``reasoning_effort`` selects its supported effort level.
                    thinking={"type": "enabled"},
                    reasoning_effort=self.reasoning_effort,
                    stream=False,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                body = getattr(exc, "body", None)
                self.last_raw_response = json.dumps(body, default=str, indent=2) if body else str(exc)
                raise LLMError(f"Z.AI request failed: {exc}") from exc

            if hasattr(completion, "model_dump_json"):
                self.last_raw_response = completion.model_dump_json(indent=2)
            else:
                self.last_raw_response = json.dumps(completion, default=str, indent=2)
            content = completion.choices[0].message.content
            try:
                result = json.loads(content or "")
                validate(instance=result, schema=schema)
                if not isinstance(result, dict):
                    raise ValidationError("top-level response is not an object")
                return result
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc).splitlines()[0]
                if attempt >= self.schema_attempts:
                    break
                LOG.warning("GLM produced invalid structured output; retrying this chunk (%d/%d)",
                            attempt + 1, self.schema_attempts)
                current_messages[0]["content"] += (
                    "\n\nYour previous response failed local JSON Schema validation: "
                    f"{last_error}. Correct the structure and return only the JSON object."
                )
        raise LLMError(f"GLM returned no schema-valid JSON after {self.schema_attempts} attempts: {last_error}")


@dataclass
class OllamaClient:
    """Minimal Ollama `/api/chat` client using only Python's standard library."""

    model: str
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: int = 600
    temperature: float = 0.0
    recommended_max_chunk_chars: int = 12_000
    provider: str = "ollama"
    last_raw_response: str | None = None

    def generate(self, messages: list[dict[str, str]], response_schema: dict[str, Any]) -> dict[str, Any]:
        self.last_raw_response = None
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": response_schema,
            "options": {"temperature": self.temperature},
        }
        http_request = request.Request(
            f"{self.base_url.rstrip('/')}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                self.last_raw_response = response.read().decode("utf-8")
                envelope = json.loads(self.last_raw_response)
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc
        try:
            content = envelope["message"]["content"]
            result = json.loads(content)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("Ollama returned no valid JSON message content") from exc
        if not isinstance(result, dict):
            raise LLMError("Ollama structured response must be a JSON object")
        return result
