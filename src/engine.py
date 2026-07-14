import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent


def load_context() -> str:
    cards = []
    for path in sorted((ROOT / "context").glob("*.md")):
        if path.name.startswith("_"):
            continue
        cards.append(f"## {path.stem}\n{path.read_text()}")
    return "\n\n".join(cards)


def build_system() -> str:
    schema = (ROOT / "framework" / "model_schema.json").read_text()
    prompt = (ROOT / "prompts" / "engine.md").read_text()
    return prompt.replace("{{SCHEMA}}", schema).replace("{{CONTEXT}}", load_context())


def run(request: str) -> dict:
    client = Anthropic()
    resp = client.messages.create(
        model=os.getenv("MODEL", "claude-sonnet-5"),
        max_tokens=4000,
        system=build_system(),
        messages=[{"role": "user", "content": request}],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    return json.loads(text)


def render(out: dict) -> None:
    r = out.get("resume_metier", {})
    print("\n=== RÉSUMÉ MÉTIER ===")
    print(f"Objectif  : {r.get('objectif', '')}")
    print(f"Périmètre : {r.get('perimetre', '')}")
    print(f"Angle mort: {r.get('angle_mort', '')}")
    if r.get("hypotheses"):
        print("Hypothèses posées :")
        for h in r["hypotheses"]:
            print(f"  - {h}")

    print("\n=== QUESTIONS PRIORITAIRES (Incertitude × Impact) ===")
    for i, q in enumerate(out.get("questions", []), 1):
        print(f"{i}. {q.get('q', '')}")
        print(f"   → [{q.get('slot', '')}] {q.get('why', '')}")

    print("\n=== ÉTAT DU MODÈLE ===")
    for slot, s in out.get("model", {}).items():
        print(
            f"  {slot:<18} {str(s.get('completeness', 0)) + '%':<5} "
            f"{s.get('confidence', ''):<9} impact={s.get('impact', '')}"
        )


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python src/engine.py "la demande client…"  |  python src/engine.py path/to/request.md')
        sys.exit(1)
    arg = sys.argv[1]
    request = Path(arg).read_text() if Path(arg).exists() else arg
    render(run(request))


if __name__ == "__main__":
    main()
