import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    load_dotenv = None

if load_dotenv:
    load_dotenv(dotenv_path=ROOT / '.env')

try:
    import google.generativeai as genai  # type: ignore
except Exception as exc:  # pragma: no cover
    raise SystemExit(f'google-generativeai is required: {exc}')

ISSUES_PATH = ROOT / 'issues.json'

CATEGORIES = [
    'BSOD stop codes (0x0000007E, 0xC000021A, 0x0000003B, 0x00000050)',
    'Windows Update errors (0x80070005, 0x8024402C, 0x800F0922)',
    'GPU and display driver failures',
    'Audio driver and sound device issues',
    'USB device not recognized or disconnecting',
    'Bluetooth pairing and connection failures',
    'High CPU usage by Windows processes',
    'High RAM usage and memory leaks',
    'Windows activation errors',
    'Microsoft Store installation failures',
    'Windows Search indexing problems',
    'Slow startup and long boot times',
    'Network adapter missing or not detected',
    'DNS resolution failures',
    'VPN connection issues',
    'Remote Desktop errors',
    'Windows Firewall blocking apps',
    'Corrupted user profile',
    'Windows Explorer crashes and freezes',
    'Task Manager not opening or responding',
]

PROMPT = '''Generate 8 Windows troubleshooting database entries for: {category}

Return ONLY a valid JSON array.
Each entry schema:
{{
  "problem": "short issue description",
  "solutions": [
    {{"command": "cmd or powershell command", "description": "what it does"}}
  ]
}}

Rules:
- Safe commands only
- No destructive actions
- At most 3 solutions per issue
'''


def normalize_issue(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    problem = str(raw.get('problem') or '').strip()
    if not problem:
        return None
    solutions = []
    for sol in raw.get('solutions', []):
        if not isinstance(sol, dict):
            continue
        command = str(sol.get('command') or '').strip()
        description = str(sol.get('description') or '').strip()
        if not command and not description:
            continue
        solutions.append({'command': command, 'description': description})
    if not solutions:
        return None
    return {'problem': problem, 'solutions': solutions[:3]}


def main() -> None:
    api_key = (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY') or '').strip()
    if not api_key:
        raise SystemExit('Missing GEMINI_API_KEY (or GOOGLE_API_KEY) in environment/.env')

    genai.configure(api_key=api_key)
    model_name = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
    model = genai.GenerativeModel(model_name)

    if ISSUES_PATH.exists():
        existing = json.loads(ISSUES_PATH.read_text(encoding='utf-8'))
    else:
        existing = []

    all_issues = list(existing)

    for category in CATEGORIES:
        print(f'Generating: {category}')
        try:
            response = model.generate_content(PROMPT.format(category=category))
            text = (response.text or '').strip()
            if text.startswith('```'):
                lines = text.splitlines()
                lines = lines[1:]
                if lines and lines[0].strip().lower() == 'json':
                    lines = lines[1:]
                if lines and lines[-1].strip() == '```':
                    lines = lines[:-1]
                text = '\n'.join(lines).strip()
            generated = json.loads(text)
            normalized = [normalize_issue(item) for item in generated]
            normalized = [item for item in normalized if item]
            all_issues.extend(normalized)
            print(f'  +{len(normalized)} issues')
        except Exception as exc:
            print(f'  FAILED: {exc}')

    deduped = []
    seen = set()
    for issue in all_issues:
        if not isinstance(issue, dict):
            continue
        key = str(issue.get('problem') or '').strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned = normalize_issue(issue)
        if cleaned:
            deduped.append(cleaned)

    ISSUES_PATH.write_text(json.dumps(deduped, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'\nTotal: {len(deduped)} issues in knowledge base')


if __name__ == '__main__':
    main()
