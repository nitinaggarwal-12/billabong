# Billabong — Clinical Coding Intelligence

Customer-demo build for urology coding audit. Billabong keeps two evidence classes visibly and structurally separate:

- **CMS rules layer** — deterministic checks sourced from CMS tables.
- **Documentation review** — optional model judgment requiring coder verification.

## Run locally

```bash
python3 seed_urology.py
python3 app.py
```

Open http://localhost:8000. Set `ANTHROPIC_API_KEY` only if you want the optional documentation-review layer enabled.

## Demo flow

1. Choose one of the synthetic sample charts.
2. Review the note and submitted codes.
3. Run the audit.
4. Show the CMS rules layer first, including authority and source.
5. Contrast it with the separately labeled documentation-review layer.
6. Use Agree / Disagree / Unsure to explain the human-review loop.

## Safety and scope

This is decision support, not autonomous coding. The application screens common identifiers and blocks the audit until detected identifiers are removed. The included sample charts are synthetic. Production PHI handling, identity/RBAC, durable audit-event storage, enterprise secrets, and a formal security/compliance review remain deployment requirements before real patient data is used.
