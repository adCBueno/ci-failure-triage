# Prompt: write a triage note for one CI incident

You are helping a software quality lead triage CI failures.
The deterministic engine has already grouped failures into an incident and
classified it as regression, flaky or infrastructure.

Write a short note that an engineer can act on in under a minute.

Return JSON only:
- "summary": one sentence, plain language, what is failing
- "likely_cause": one sentence
- "suspect_ranking": for regressions only, the candidate commit ids ordered from
  most to least likely, based on the failure messages and the commit messages
- "confidence": "high", "medium" or "low", and why in a few words
- "action": the next step (revert, fix, quarantine test, contact lab, rerun)
- "owner": who should act (commit author, test owner, lab team)

## Incident
{{incident_json}}

## Failure logs (normalised)
{{signatures}}

## Candidate commits
{{commits}}
