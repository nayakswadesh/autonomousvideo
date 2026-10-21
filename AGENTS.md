# Autonomous Studio maintenance

When a live project fails, do not issue an unchanged retry as the default response.
Inspect the failed artifact/provider output and identify the upstream cause: generation
contract, validated schema, repair targeting, stale state, or external service.
Where the cause is in the studio, fix the topic-agnostic framework and add a
regression test before retrying the project. Keep approved human decisions intact;
use a versioned revision when the workflow requires new creative material. Report
what changed, what was tested, and the next approval gate. Do not auto-approve a
script, visual plan, or prototype without the user's explicit decision.
