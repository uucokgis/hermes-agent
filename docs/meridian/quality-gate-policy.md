# Meridian Quality Gate Policy

This policy defines how raw scanner output is normalized for Matthew.

The goal is not to block on every tool complaint.
The goal is to convert noisy tool output into review evidence buckets:

- `blocking`
- `review`
- `debt`
- `advisory`

## Tool Policy

### Build and test

- backend `pytest` failures -> `blocking`
- frontend build failures -> `blocking`

These usually mean the handoff is not review-ready.

### Lint and style

- Ruff format/lint failures -> `review`
- ESLint failures -> `review`

These are normally request-changes material, not automatic hard blockers unless they hide a deeper defect.

### Dependency vulnerabilities

- `pip-audit` findings in critical runtime packages such as `django`, `djangorestframework`, `djangorestframework-simplejwt`, `cryptography`, or `pillow` -> `review`
- remaining dependency vulnerabilities -> `debt`

This keeps dependency backlog visible without forcing Matthew to block every review on old package debt.

### Bandit

- high severity -> `blocking`
- medium severity -> `review`
- low severity -> `advisory`

### Semgrep

- injection, auth bypass, password-validation, and similarly dangerous rules -> `blocking`
- weaker security smells such as insecure hashes, unsafe transport, or HTML/template hazards -> `review`

### Frontend security script

- findings that clearly indicate higher severity wording -> `review`
- remaining findings -> `debt`

## Review Contract

- `blocking` means Matthew should normally return the task to Fatih unless there is a very strong reason not to.
- `review` means Matthew must inspect and judge contextually.
- `debt` means the signal is real enough to preserve, but not necessarily enough to block the current task.
- `advisory` means useful caution or hygiene signal only.

The scanner is evidence, not the final reviewer.
Matthew still decides the review outcome.
