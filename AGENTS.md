# Repository Agent Instructions & Git Workflow

## Skill Overrides & Routing

This repository-level configuration overrides the global `github-workflows` / `git-workflow` skill. All agents operating within this workspace must adhere to the Git workflow and branch rules defined below.

---

## Git Workflow

### 1. Branch Rules
- **Active Branch**: Exclusively work and push directly to `dev`.
- **Protected Branch (`main`)**: NEVER push directly to `main`.
- **Promotion to `main`**: `main` is updated exclusively via Pull Requests from `dev`.
- **No Agent Merges**: Agents must not execute merges into `main` locally or remotely.

### 2. Commits
Commit messages MUST be concise, simple, specific, and prefixed with a conventional category:
- **Allowed Prefixes**: `feat:`, `fix:`, `test:`, `refactor:`, `docs:`, `chore:`, `build:`, `ci:`, `perf:`, `style:`
- **Examples**:
  - `feat: add receipt parser for costco`
  - `fix: correct tax calculation on split items`
  - `test: add unit test for zero-eligible allocations`
  - `docs: update setup instructions in README`
- **Prohibited Messages**: Vague descriptions such as `update stuff`, `changes`, `wip`, or `fix code`.

### 3. Verification & Safety
- Verify working tree and active branch before committing (`git status`).
- Never claim a Git operation succeeded unless verified via command output.
- Never commit secrets, credentials, or `.env` files.
