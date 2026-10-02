# Security Test Report

## Summary

The application enforces authentication and authorisation on the main API surfaces, and the server-side code uses password hashing and JWT validation. The project also contains explicit privacy guards to reject video uploads and mobile-only raw-frame data.

## Verified findings

- PASS: Password hashing is handled server-side through the app’s auth flow and password-based demo user creation.
- PASS: JWT access/refresh tokens are generated and validated by the backend auth stack.
- PASS: Role-based access enforcement exists for citizen, volunteer, cop, and admin flows.
- PASS: Complaint and sighting endpoints explicitly reject video/raw-frame uploads via the privacy middleware in [backend/app/main.py](../backend/app/main.py).
- PASS: Sensitive URLs and backend configuration are isolated in environment settings rather than hardcoded in business logic.

## Areas that remain partially or manually validated

- PARTIAL: Rate limiting, CSRF, and deeper security hardening were not exhaustively probed under load in this environment.
- PARTIAL: Production secret management was not fully audited for all deployment environments because this workspace does not include a full external deployment configuration.
- NOT TESTABLE: Full Docker security posture could not be evaluated because Docker is unavailable in this environment.

## Security notes

- Secrets must remain in `.env` files or environment injection only and should not be committed to the repository.
- The project already uses environment-based configuration for credentials, which is the correct pattern.
