"""Command-line interface for the complete reproducible pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

if __package__ in {None, ""}:
    # Support PyCharm's "Script path" launch mode as well as `python -m`.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from audit_pipeline.contract_agent import ContractUnderstandingAgent, source_fingerprint
    from audit_pipeline.chunking import chunk_contract, save_chunks
    from audit_pipeline.contract_parser import write_contract
    from audit_pipeline.engine import audit
    from audit_pipeline.evaluation import evaluate
    from audit_pipeline.io import SUBMISSION_FIELDS, read_csv, write_csv, write_json
    from audit_pipeline.llm import GLMClient, GroqClient, OllamaClient
else:
    from .contract_agent import ContractUnderstandingAgent, source_fingerprint
    from .chunking import chunk_contract, save_chunks
    from .contract_parser import write_contract
    from .engine import audit
    from .evaluation import evaluate
    from .io import SUBMISSION_FIELDS, read_csv, write_csv, write_json
    from .llm import GLMClient, GroqClient, OllamaClient


LOG = logging.getLogger("insurance_audit")


def _hospital_number(value: str) -> int:
    value = value.lower().replace("hospital_", "").replace("h", "")
    number = int(value)
    if number not in range(1, 6):
        raise argparse.ArgumentTypeError("hospital must be 1..5")
    return number


def run(root: Path, output: Path, hospitals: list[int], do_evaluate: bool = True,
        llm_provider: str = "glm", llm_model: str | None = None,
        ollama_url: str = "http://127.0.0.1:11434", cache_dir: Path | None = None,
        max_chunk_chars: int | None = None,
        max_completion_tokens: int | None = None) -> None:
    output.mkdir(parents=True, exist_ok=True)
    all_submission = []
    cache_dir = cache_dir or output / "contract_cache"
    if llm_provider == "glm":
        client = GLMClient(model=llm_model or "glm-5.3")
        if max_completion_tokens is not None:
            client.max_completion_tokens = max_completion_tokens
    elif llm_provider == "groq":
        client = GroqClient(model=llm_model or "openai/gpt-oss-20b")
        if max_completion_tokens is not None:
            client.max_completion_tokens = max_completion_tokens
    else:
        client = OllamaClient(model=llm_model or "gpt-oss:20b", base_url=ollama_url)
    contract_agent = ContractUnderstandingAgent(
        root=root, cache_dir=cache_dir,
        prompt_path=root / "config" / "prompts" / "contract_extraction_v5.md",
        llm_client=client, chunks_dir=output / "chunks",
        llm_runs_dir=output / "llm_runs",
        max_chunk_chars=max_chunk_chars or getattr(client, "recommended_max_chunk_chars", 12_000),
    )
    for number in hospitals:
        hid = f"H{number}"
        LOG.info("parsing contract for %s", hid)
        contract = contract_agent.parse(root / "data" / "contracts" / f"hospital_{number}", hid)
        write_contract(contract, output / "contracts" / f"hospital_{number}.json")
        invoices = read_csv(root / "data" / "invoices" / f"hospital_{number}_invoices.csv")
        lines = read_csv(root / "data" / "invoices" / f"hospital_{number}_line_items.csv")
        LOG.info("auditing %d invoices and %d lines for %s", len(invoices), len(lines), hid)
        predictions, details = audit(contract, invoices, lines)
        write_csv(output / f"hospital_{number}_predictions.csv", predictions, SUBMISSION_FIELDS)
        write_json(output / "audit_details" / f"hospital_{number}.json", details)
        if number > 1:
            all_submission.extend(predictions)
        if number == 1 and do_evaluate:
            report, markdown = evaluate(
                predictions, root / "data" / "labels" / "hospital_1_labels.csv",
            )
            write_json(output / "hospital_1_evaluation.json", report)
            (output / "hospital_1_evaluation.md").write_text(markdown, encoding="utf-8")
            LOG.info("Hospital 1 accuracy %.1f%%, F1 %.1f%%", report["classification_accuracy"] * 100, report["f1"] * 100)
    if all_submission:
        write_csv(output / "submission.csv", all_submission, SUBMISSION_FIELDS)
        LOG.info("wrote %d submission rows", len(all_submission))


def main() -> None:
    default_root = Path(__file__).resolve().parents[1]
    load_dotenv(default_root / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root,
                        help="repository root; defaults to the project containing audit_pipeline")
    parser.add_argument("--output", type=Path, default=None,
                        help="generated artifact directory; defaults to <root>/artifacts")
    parser.add_argument("--hospitals", type=_hospital_number, nargs="+", default=[1],
                        help="hospital numbers to process; defaults to Hospital 1")
    parser.add_argument("--no-evaluate", action="store_true")
    parser.add_argument("--llm-provider", choices=("glm", "groq", "ollama"),
                        default=os.getenv("LLM_PROVIDER", "glm"))
    parser.add_argument("--llm-model", default=os.getenv("LLM_MODEL"),
                        help="defaults to glm-5.3 for GLM, openai/gpt-oss-20b for Groq, or gpt-oss:20b for Ollama")
    parser.add_argument("--ollama-url", default=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--max-chunk-chars", type=int,
                        default=int(os.getenv("CONTRACT_MAX_CHUNK_CHARS", "0")) or None,
                        help="override the provider-aware contract chunk size")
    parser.add_argument("--max-completion-tokens", type=int,
                        default=int(os.getenv("LLM_MAX_COMPLETION_TOKENS", os.getenv("GROQ_MAX_COMPLETION_TOKENS", "0"))) or None,
                        help="override the cloud provider's maximum structured-output tokens")
    parser.add_argument("--inspect-chunks", action="store_true",
                        help="print the contract chunk plan and exit without calling an LLM")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    root = args.root.resolve()
    output = args.output.resolve() if args.output else root / "artifacts"
    if args.inspect_chunks:
        provider = args.llm_provider
        provider_default = 6000 if provider == "groq" else 12_000
        max_chars = args.max_chunk_chars or provider_default
        for number in args.hospitals:
            paths = sorted((root / "data" / "contracts" / f"hospital_{number}").glob("*.md"))
            chunks = chunk_contract(paths, max_chars)
            saved_at = save_chunks(chunks, output / "chunks", f"H{number}", source_fingerprint(paths), max_chars, 20)
            print(f"Hospital {number}: {len(chunks)} chunks saved to {saved_at}")
            for chunk in chunks:
                print(f"  {chunk.chunk_id} | {chunk.source_file} | {chunk.char_count} chars | {'; '.join(chunk.sections)} | {', '.join(chunk.content_types)}")
        return
    run(root, output, args.hospitals, not args.no_evaluate,
        args.llm_provider, args.llm_model, args.ollama_url,
        args.cache_dir.resolve() if args.cache_dir else None,
        args.max_chunk_chars, args.max_completion_tokens)


if __name__ == "__main__":
    main()
