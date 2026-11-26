#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f go.mod || ! -f main.go ]]; then
  echo "ERROR: run this script from the watchlab-pilot project directory (where go.mod and main.go are)." >&2
  exit 1
fi
if [[ ! -f recover.go || ! -f recover_test.go ]]; then
  echo "ERROR: recover.go and recover_test.go must be in the current directory." >&2
  exit 1
fi

python3 - <<'PY'
from pathlib import Path

path = Path("main.go")
source = path.read_text(encoding="utf-8")
replacements = [
    (
        'return errors.New("expected command: produce or watch")',
        'return errors.New("expected command: produce, watch or recover")',
    ),
    (
        '\tcase "watch":\n\t\treturn runWatch(args[1:])\n\tcase "help", "-h", "--help":',
        '\tcase "watch":\n\t\treturn runWatch(args[1:])\n\tcase "recover":\n\t\treturn runRecover(args[1:])\n\tcase "help", "-h", "--help":',
    ),
    (
        '  watchlab watch   [flags]\n\nCommands:',
        '  watchlab watch   [flags]\n  watchlab recover [flags]\n\nCommands:',
    ),
    (
        '  watch    Subscribe to an event prefix and record Watch responses as JSONL.\n\nUse',
        '  watch    Subscribe to an event prefix and record Watch responses as JSONL.\n  recover  Rebuild local state with Range and continue with Watch; resnapshot after compaction.\n\nUse',
    ),
]

for old, new in replacements:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"Refusing to patch main.go: expected one occurrence, found {count}: {old!r}")
    source = source.replace(old, new, 1)

path.write_text(source, encoding="utf-8")
print("Updated main.go: registered the recover command.")
PY

gofmt -w main.go recover.go recover_test.go

echo
echo "=== TESTS ==="
go test ./...
echo
echo "=== BUILD ==="
mkdir -p bin
go build -o bin/watchlab .
echo
echo "SUCCESS: recover command installed and binary rebuilt."
