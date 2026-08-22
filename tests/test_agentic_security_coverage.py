"""Coverage-and-validation-discipline additions: skill content, finish_scan gate, briefing text."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from strix.skills import get_available_skills, load_skills
from strix.tools.finish.tool import finish_scan


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_merge_briefings() -> ModuleType:
    module_path = REPO_ROOT / "docs" / "merge_briefings.py"
    spec = importlib.util.spec_from_file_location("merge_briefings", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_coverage_skill_is_discoverable() -> None:
    available = get_available_skills()
    names = {skill["name"] for skill in available.get("vulnerabilities", [])}
    assert "agentic_security_coverage" in names


def test_coverage_skill_loads_and_defines_states() -> None:
    loaded = load_skills(["agentic_security_coverage"])
    body = loaded["agentic_security_coverage"]

    for state in ("CONFIRMED", "NOT_VULNERABLE", "INCONCLUSIVE", "NOT_APPLICABLE", "NOT_TESTED"):
        assert state in body

    assert "Jailbreak" in body
    assert "Data Exfiltration" in body
    assert "Capability Abuse" in body
    assert "Context Manipulation" in body
    assert "fabricated attack ID" in body or "fake" in body.lower()


def test_agentdojo_and_injecagent_skills_reference_coverage_skill() -> None:
    loaded = load_skills(["agentdojo", "injecagent"])
    assert "agentic_security_coverage" in loaded["agentdojo"]
    assert "agentic_security_coverage" in loaded["injecagent"]


def test_finish_scan_docstring_has_coverage_completeness_gate() -> None:
    docstring = finish_scan.description or ""

    assert "Coverage completeness gate" in docstring
    for state in ("CONFIRMED", "NOT_VULNERABLE", "INCONCLUSIVE", "NOT_APPLICABLE", "NOT_TESTED"):
        assert state in docstring
    assert "the same thing as" in docstring
    assert "CONFIRMED`` once a" in docstring


def test_merge_briefings_output_includes_coverage_discipline() -> None:
    merge_briefings = _load_merge_briefings()

    rendered = merge_briefings.render_combined(
        [
            {
                "source": "agentdojo",
                "target_profile": "profile text",
                "worked_examples": ["example a"],
            },
            {
                "source": "injecagent",
                "target_profile": "profile text 2",
                "worked_examples": ["example b"],
            },
        ]
    )

    assert "## Coverage discipline" in rendered
    assert "agentic_security_coverage" in rendered
    assert "NOT_TESTED" in rendered
    assert "An attack attempt is not a finding" in rendered
