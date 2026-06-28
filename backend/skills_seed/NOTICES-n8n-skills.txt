NOTICES

n8n-mcp-skills
Copyright (c) Romuald Członkowski (AiAdvisors)
Licensed under the MIT License (see LICENSE).

------------------------------------------------------------------------
This product includes software and documentation adapted from:

  n8n-io/skills
  https://github.com/n8n-io/skills
  Copyright n8n GmbH and contributors
  Licensed under the Apache License, Version 2.0
  https://www.apache.org/licenses/LICENSE-2.0

A copy of the Apache License 2.0 is included at NOTICES-APACHE-2.0.txt.
------------------------------------------------------------------------

The following files are adapted from n8n-io/skills, retargeted for the
community n8n-mcp MCP server (different tool names, parameter paths, and
node-type forms), with reminder text rewritten to reference this pack's
skills and the marker/cache directories changed to a distinct namespace:

  hooks/hooks.json
  hooks/session-start.sh
  hooks/pre-tool-use/_emit.sh
  hooks/pre-tool-use/get-node.sh
  hooks/pre-tool-use/create-workflow.sh
  hooks/pre-tool-use/update-workflow.sh
  hooks/pre-tool-use/validate-workflow.sh
  hooks/pre-tool-use/test-workflow.sh
  hooks/pre-tool-use/instances.sh
  hooks/pre-tool-use/manage-credentials.sh
  hooks/post-tool-use/validate-workflow.sh

Notable changes from upstream:
  - Hooks match the n8n-mcp tool names (get_node, validate_workflow,
    n8n_create_workflow, n8n_update_partial_workflow, n8n_validate_workflow,
    n8n_test_workflow) instead of the first-party MCP names.
  - The PreToolUse get_node hook reads a single SHORT-form nodeType; the
    PostToolUse validate hook parses workflow JSON node types (LONG form)
    rather than grepping SDK source code.
  - The upstream execute_workflow hook was dropped (no such tool on this
    server) and its error-handling reminder folded into the test_workflow hook.
  - PreToolUse hooks for n8n_instances and n8n_manage_credentials were added
    (original to this pack, reusing the adapted _emit.sh one-shot pattern) to
    surface the multi-instance and credential-discipline guidance.

The skills/using-n8n-mcp-skills router borrows the document *structure* of
the upstream using-n8n-skills meta-skill; its prose is original to this
repository.

This repository as a whole is licensed under the MIT License (see LICENSE).
The adapted content above is included in compliance with Apache License 2.0,
Section 4. No attribution notices are placed inside agent-facing skill content
(SKILL.md or reference files); attribution lives here, in README.md, and in the
header comments of the adapted hook scripts.
