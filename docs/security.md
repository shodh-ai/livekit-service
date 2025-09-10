# Security

## Credentials and tokens
- LiveKit API key/secret grant powerful access. Store in secret manager, mount as env at runtime.
- DEEPGRAM_API_KEY also sensitive.
- Never log tokens or secrets. The code avoids logging full secrets; verify logs in your environment.

## Frontend RPC trust model
- RPC to the viewer uses the LiveKit room transport; only authenticated room participants can receive actions.
- UI actions are hints/commands; the viewer client should validate and sanitize parameters.

## Browser pod data channel
- Commands are published to all participants but include a `target` identity; the browser pod filters by target and `source`="agent".
- Consider adding signing or additional validation in the browser pod if needed.

## Network access
- Restrict egress to only required hosts: LiveKit, LangGraph, Deepgram, and internal services.

## Container hardening
- Use minimal base images (Dockerfile uses python:3.11-slim).
- Drop capabilities, run as non‑root where possible (adjust base image if required).
- Keep dependencies pinned (see `requirements.txt`).

## Endpoint exposure
- `/dev/send-browser-command` and `/local-spawn-agent` are development helpers; avoid exposing them to the public Internet.
- Use private networking, firewall rules, or service auth to restrict access.
