/**
 * Per-view coachmarks. Each tour leads with "what this place is for," then points
 * at the one or two controls that aren't self-evident.
 *
 * The list views (Overview, Workflows, Executions/Errors, Containers) get a
 * single orienting bubble so a newcomer always gets a "you are here" on first
 * visit. The workspaces a new operator wouldn't otherwise discover (Code Lab,
 * the Harness, MCP, Models, Secrets) get a short multi-step walkthrough.
 *
 * Anchors are stable selectors already present in each view. Steps whose anchor
 * is missing are skipped by the engine, so empty states never break a tour.
 */

export const TOURS = {
  // No Overview/dashboard tour on purpose: the dashboard is the wizard's landing
  // spot, where the get-started card and the post-wizard "Connect your n8n" guide
  // already claim attention. A coachmark here collided with that guide. The
  // dashboard is self-explanatory, so it gets no bubble.
  workflows: [
    { anchor: '.section-title', title: 'Workflows', body: 'Every workflow on the active n8n instance, with status and quick actions. Switch instances from the sidebar to see another fleet member.', placement: 'bottom' },
  ],
  errors: [
    { anchor: '.section-title', title: 'Executions & errors', body: 'Recent workflow failures land here automatically once the error handler is installed. Group, inspect, and ask AI to triage the root cause.', placement: 'bottom' },
  ],
  containers: [
    { anchor: '.section-title', title: 'Containers', body: 'Deploy and manage Docker services right from the dashboard: n8n, databases, and more, into the daemon running this app.', placement: 'bottom' },
    { anchor: '#ct-template-grid', title: 'One-click deploy', body: 'Pick a template to stand up a service. Passwords are auto-generated; the new container shows up in the list below to start, stop, or destroy.', placement: 'top' },
  ],
  codelab: [
    { anchor: '#monaco-container', title: 'This is Code Lab', body: 'Write and test n8n Code-node JavaScript or Python with AI help, then push it straight into a workflow.', placement: 'right' },
    { anchor: '#mode-code', title: 'Two modes', body: 'Write a Code-node snippet, or switch to Workflow Builder to generate whole-workflow JSON from a description.', placement: 'bottom' },
    { anchor: '#codelab-prompt-builder', title: 'Prompt Builder', body: 'Assemble a structured agent system prompt (role, rules, tools, output format) and drop it straight into your code or an n8n AI Agent node.', placement: 'bottom' },
    { anchor: '#codelab-provider', title: 'Pick your model', body: 'Override the provider and model for this session. Defaults come from Models.', placement: 'left' },
    { anchor: '#code-send-btn', title: 'Send to n8n', body: 'One click drops the current code into a new workflow on the active instance.', placement: 'bottom' },
  ],
  instances: [
    { anchor: '.section-title', title: 'Your n8n instances', body: 'Connect and manage every n8n you run, self-hosted or cloud. The colored dot marks the active one; click it to switch which instance the whole dashboard targets.', placement: 'bottom' },
    { anchor: '#inst-add-area', title: 'Add an instance', body: 'Point AgeniusDesk at another n8n with its URL and an API key. Managed containers can be updated in place from here too.', placement: 'left' },
  ],
  'ai-settings': [
    { anchor: '.section-title', title: 'AI models', body: 'Each assistant area (Code Lab, error triage, chat) picks its own provider and model independently, and carries its own instructions. API keys come from Secrets, referenced by provider.', placement: 'bottom' },
  ],
  'mcp-servers': [
    { anchor: '.section-title', title: 'MCP servers', body: 'Connect MCP servers to give the assistant external tools: databases, APIs, knowledge bases. The merged tool inventory shows up right below.', placement: 'bottom' },
    { anchor: '#add-mcp-btn', title: 'Add a server', body: 'Add an MCP endpoint and optional auth token, then scope it to a specific n8n instance or make it global.', placement: 'left' },
  ],
  knowledge: [
    { anchor: '.section-title', title: 'This is the Harness', body: 'Every file and source your agents work from lives here, the workspace behind the assistant.', placement: 'bottom' },
    { anchor: '#ku-config', title: 'Sources, connectors & instructions', body: 'Expand this for external knowledge to search, MCP-backed connectors, and the agent rules (AGENTS.md) every assistant follows.', placement: 'bottom' },
    { anchor: '#ku-vault', title: 'Your notes vault', body: 'Markdown files the assistant can read and write, with search, backlinks, and tags.', placement: 'top' },
  ],
  secrets: [
    { anchor: '.section-title', title: 'Encrypted secrets', body: 'Store API keys and tokens once, encrypted at rest. Reference them anywhere as $NAME, the values never leave the server.', placement: 'bottom' },
    { anchor: '#add-secret-form', title: 'Add a secret', body: 'Name it (e.g. ANTHROPIC_KEY), pick a type, and paste the value. Then use $ANTHROPIC_KEY in any field.', placement: 'left' },
    { anchor: '#secrets-list', title: 'Your store', body: 'Everything you have saved. Copy its $reference, sync a credential straight into any n8n instance, or remove it.', placement: 'right' },
  ],
  insights: [
    { anchor: '.view-header', title: 'Your insights summary', body: 'A roll-up of execution analytics across all workflows on the active instance: success rates, busiest and slowest workflows, and error trends.', placement: 'bottom' },
  ],
  admin: [
    { anchor: '.section-title', title: 'Admin', body: 'Manage who can access n8n and the dashboard, plus install-level system settings.', placement: 'bottom' },
    { anchor: '.tab-btn[data-tab="n8n-users"]', title: 'n8n instance users', body: 'Invite teammates into the active n8n instance and set their roles.', placement: 'bottom' },
    { anchor: '.tab-btn[data-tab="dashboard-users"]', title: 'Dashboard users', body: 'Control who can sign into AgeniusDesk itself.', placement: 'bottom' },
    { anchor: '.tab-btn[data-tab="system"]', title: 'System', body: 'Environment variables, license activation, and install-level settings.', placement: 'bottom' },
  ],
  import: [
    { anchor: '.section-title', title: 'Import workflows', body: 'Bring workflows into n8n from JSON, one at a time or in bulk.', placement: 'bottom' },
    { anchor: '#import-title', title: 'Options', body: 'Optionally rename and tag workflows as they come in, and choose which instance to import to.', placement: 'right' },
    { anchor: '#drop-zone', title: 'Upload JSON', body: 'Drop or pick one or more .json workflow files to import them.', placement: 'top' },
    { anchor: '#json-paste', title: 'Or paste JSON', body: 'Paste raw workflow JSON here and import it directly.', placement: 'top' },
  ],
  backup: [
    { anchor: '.section-title', title: 'Export & backup', body: 'Export workflows as JSON for backup or version control, and restore them later.', placement: 'bottom' },
    { anchor: '#backup-all-btn', title: 'Full backup', body: 'Download every workflow (or just the active ones) as a single backup file.', placement: 'bottom' },
    { anchor: '#workflow-checklist', title: 'Export individual', body: 'Or pick specific workflows and export just those.', placement: 'top' },
    { anchor: '#restore-drop-zone', title: 'Restore', body: 'Drop a backup file here to restore those workflows into the active instance.', placement: 'top' },
  ],
  settings: [
    { anchor: '.tab-btn[data-tab="account"]', title: 'Your account', body: 'Change your password, turn on two-factor, and manage active sessions.', placement: 'bottom' },
    { anchor: '.tab-btn[data-tab="themes"]', title: 'Themes', body: 'Switch the dashboard look, or load a custom theme.', placement: 'bottom' },
    { anchor: '.tab-btn[data-tab="help"]', title: 'Help & tips', body: 'Replay any page tour, reopen the setup wizard, or toggle these tips off.', placement: 'bottom' },
  ],
};
