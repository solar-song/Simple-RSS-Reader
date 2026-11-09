Skill: git-commands

Purpose
- Record the exact git commands and a small helper script to commit and push changes for this project.

Usage
1. Quick commit + push with a message:
   ./scripts/git-commands.sh "Your commit message"

Commands the script runs
- git add -A
- git commit -m "$1"
- git push -u origin main

Notes
- You must have credentials configured for the remote (SSH key or cached HTTPS credentials).
- This helper is intentionally minimal. Review the commit message before pushing.
