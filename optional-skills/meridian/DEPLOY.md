# Meridian Skills — Deploy Rehberi

Bu klasördeki skill'leri 106 makinesindeki Hermes'e deploy etmek için:

```bash
# Mac'ten çalıştır
SKILLS_DIR="/Users/umut/Projects/hermes-agent/optional-skills/meridian"
REMOTE="umut@192.168.1.106"
REMOTE_DIR="~/.hermes/skills/meridian"

ssh $REMOTE "mkdir -p $REMOTE_DIR/pm $REMOTE_DIR/developer $REMOTE_DIR/reviewer"

scp $SKILLS_DIR/pm/SKILL.md       $REMOTE:$REMOTE_DIR/pm/SKILL.md
scp $SKILLS_DIR/developer/SKILL.md $REMOTE:$REMOTE_DIR/developer/SKILL.md
scp $SKILLS_DIR/reviewer/SKILL.md  $REMOTE:$REMOTE_DIR/reviewer/SKILL.md

echo "Deploy tamamlandı."
```

## Skill Tetikleyicileri

| Skill | Persona | Tetikleyici |
|---|---|---|
| pm/SKILL.md | Philip | "meridian'da gez", "pm olarak bak", "task aç" |
| developer/SKILL.md | Fatih | "developer olarak çalış", "task al", "kodu yaz" |
| reviewer/SKILL.md | Matthew | "reviewer olarak bak", "PR'ı review et" |

## Meridian Docs Deploy (107'ye)

```bash
MERIDIAN_DOCS="/Users/umut/Projects/hermes-agent/docs/meridian"
REMOTE107="umut@192.168.1.107"
MERIDIAN_PROJ="/home/umut/meridian/docs/llm"

ssh $REMOTE107 "mkdir -p $MERIDIAN_PROJ"
scp $MERIDIAN_DOCS/agentic-workflow.md $REMOTE107:$MERIDIAN_PROJ/agentic-workflow.md
scp $MERIDIAN_DOCS/agent-prompts.md    $REMOTE107:$MERIDIAN_PROJ/agent-prompts.md
```
