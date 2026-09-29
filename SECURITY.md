# Security Policy

## Supported versions

Only the latest commit on `main` is supported. There are no release branches.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Report privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Include:

- what you found and where (file, function, request),
- steps or a minimal payload to reproduce it,
- the impact you expect.

You should get an acknowledgement within 5 working days. Confirmed issues are fixed on `main` with a regression test, and the reporter is credited unless they ask otherwise.

## Scope

Agentproof is a research lab, not a payments system. All keys, mandates and traffic are synthetic. The HMAC keys in the code are test fixtures; never reuse them anywhere. The FastAPI service has no authentication and is meant for local use only.
