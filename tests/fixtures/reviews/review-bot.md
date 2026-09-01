### Security review

No high-severity issues found. One note: `src/api/retry.py` logs the full request headers at debug level, which will include the Authorization header.

Status: PASS with notes
