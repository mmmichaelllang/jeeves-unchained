#!/usr/bin/env python3
"""Send a permanent-pipeline-failure alert email.

Invoked by the auto-retry classifier in .github/workflows/retry-failed.yml when
a Daily Pipeline failure is classified as permanent (auth, import, secret
missing). Reads context from environment so the workflow can pass classifier
output without inline-quoting it.

Env (all set by the workflow):
    GMAIL_APP_PASSWORD          required — used by jeeves.email.send_html
    JEEVES_RECIPIENT_EMAIL      required — alert recipient
    PIPELINE_REASON             required — classifier reason
    PIPELINE_SAMPLE             optional — first matched log line (truncated)
    PIPELINE_RUN_URL            optional — link to the failed run
    PIPELINE_WORKFLOW           optional — workflow display name
    PIPELINE_ATTEMPT            optional — attempt number from workflow_run

Exits 0 on success or graceful skip; non-zero only on an unexpected crash.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("jeeves.notify_failure")

    reason = os.environ.get("PIPELINE_REASON", "(no reason supplied)")
    sample = os.environ.get("PIPELINE_SAMPLE", "")
    run_url = os.environ.get("PIPELINE_RUN_URL", "")
    workflow = os.environ.get("PIPELINE_WORKFLOW", "Daily Pipeline")
    attempt = os.environ.get("PIPELINE_ATTEMPT", "?")

    details_lines = [
        f"Workflow: {workflow}",
        f"Attempt:  {attempt}",
    ]
    if run_url:
        details_lines.append(f"Run URL:  {run_url}")
    if sample:
        details_lines.append("")
        details_lines.append("First matched log line:")
        details_lines.append(sample)

    remediation = (
        "1. Open the run URL above and read the failed-step log.\n"
        "2. If the marker mentions invalid_grant or OAuth: run\n"
        "       python scripts/gmail_auth.py --credentials ~/Downloads/credentials.json\n"
        "   then update the GitHub secret GMAIL_OAUTH_TOKEN_JSON.\n"
        "3. If the marker mentions Missing required environment variables:\n"
        "   verify the named secrets in repo Settings -> Secrets and variables.\n"
        "4. If the marker mentions ImportError / ModuleNotFoundError /\n"
        "   SyntaxError: revert the most recent main commit; a bad change shipped.\n"
        "5. Re-trigger the pipeline via daily.yml workflow_dispatch."
    )

    try:
        from jeeves.alert import send_failure_alert
    except ImportError as exc:
        log.error("could not import jeeves.alert: %s", exc)
        return 0  # alert is best-effort; do not fail CI

    sent = send_failure_alert(
        subject=f"{workflow} — permanent failure (no auto-retry)",
        reason=reason,
        details="\n".join(details_lines),
        remediation=remediation,
    )
    log.info("alert email %s", "sent" if sent else "NOT sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
