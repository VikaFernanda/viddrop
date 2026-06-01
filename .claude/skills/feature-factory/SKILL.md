---
name: feature-factory
description: Use this skill when the user asks to build, ship, or implement 
a feature end to end in Viddrop. Runs the full chain of seven subagents 
with human approval points after the story and after the brief. 
Triggers on: "build a feature", "ship a feature", "feature factory", 
"run the factory", "implement X".
---

Process:

1. Invoke codebase-researcher. Pass the feature idea and the relevant area 
   of code. Wait for findings.

2. Invoke story-writer. Pass the feature idea and the researcher's findings. 
   Wait for the user story.

3. PAUSE — Show the story to the user:
   "Does this match what you want? Reply 'approved' to continue, 
   describe what to change, or 'reject' to stop."
   - Approved → continue.
   - Changes requested → re-invoke story-writer with feedback. Repeat until approved or rejected.
   - Rejected → stop. Summarise what was explored.

4. Invoke tech-brief-writer. Pass the approved story and the researcher's findings. 
   Wait for the technical brief.

5. PAUSE — Show the brief to the user:
   "Any design or security red flags? Reply 'approved' to continue, 
   describe what to change, or 'reject' to stop."
   - Approved → continue.
   - Changes requested → re-invoke spec-writer with feedback. Repeat until approved or rejected.
   - Rejected → stop. Keep the approved story.

6. Invoke backend-builder. Pass the brief and the researcher's findings.
   Wait for the backend implementation and its summary.

7. Invoke frontend-builder. Pass the brief, researcher's findings, and the 
   backend-builder's summary (API/signals contract). Wait for frontend 
   implementation and its summary.

8. Invoke test-verifier. Pass the approved story, brief, and both builder 
   summaries. Wait for acceptance tests and the verifier's report.

9. Invoke implementation-validator. Pass the approved story, brief, 
   test-verifier's report, and current implementation. Wait for findings.

10. If the validator reports critical findings:
    - Route to backend-builder or frontend-builder with the finding + failing test.
    - Re-run test-verifier.
    - Re-run implementation-validator.

11. PAUSE — Show the validator findings to the user:
    "Ready to open the PR? Any findings you want to waive?"

Rules:
- Never skip the human approval points.
- Never invoke frontend-builder before backend-builder finishes.
- Never invoke test-verifier before both builders have finished.
- Never invoke the validator before test-verifier has run.
- If any agent cannot complete its task, stop and surface the reason.
- Security findings are always critical. Never waive them silently.
