"""Curated list of common n8n credential type names + display-name overrides.

We can't list all credential types via n8n's public API — no `/api/v1/credentials/types`
endpoint exists. The public API only exposes `/api/v1/credentials/schema/{type}`,
which returns the JSON schema for a specific type by name.

So we keep a curated roster here. At request time, we fetch each of these from
the target n8n instance's schema endpoint in parallel — types the instance
doesn't ship (404) are filtered out automatically, so the UI always reflects
what the user's actual n8n version actually supports.

Add new types here as users need them; schema fetching handles the field-shape
automatically (no need for per-type overrides unless the schema has quirks like
anthropicApi's allOf conditional — those go in mappings.CRED_TYPES).
"""
from __future__ import annotations

# (type_name, display_name, name_patterns_for_auto_detect)
# Patterns are substring-matched case-insensitive against the AgeniusDesk
# secret name to auto-select the dropdown value. First match wins.
KNOWN_TYPES: list[tuple[str, str, list[str]]] = [
    # LLM providers
    ("anthropicApi",      "Anthropic API",             ["ANTHROPIC", "CLAUDE"]),
    ("openAiApi",         "OpenAI API",                ["OPENAI"]),
    ("openRouterApi",     "OpenRouter API",            ["OPENROUTER", "OPEN_ROUTER"]),
    ("mistralCloudApi",   "Mistral Cloud API",         ["MISTRAL"]),
    ("cohereApi",         "Cohere API",                ["COHERE"]),
    ("deepSeekApi",       "DeepSeek API",              ["DEEPSEEK"]),
    ("groqApi",           "Groq API",                  ["GROQ"]),
    ("perplexityApi",     "Perplexity API",            ["PERPLEXITY"]),
    ("huggingFaceApi",    "Hugging Face API",          ["HUGGINGFACE", "HF_TOKEN", "HF_KEY"]),
    ("elevenLabsApi",     "ElevenLabs API",            ["ELEVENLABS", "ELEVEN_LABS"]),

    # Messaging / social
    ("telegramApi",       "Telegram API",              ["TELEGRAM"]),
    ("slackApi",          "Slack API",                 ["SLACK_TOKEN", "SLACK_API"]),
    ("slackOAuth2Api",    "Slack OAuth2",              ["SLACK_OAUTH"]),
    ("discordWebhookApi", "Discord Webhook",           ["DISCORD_WEBHOOK"]),
    ("discordBotApi",     "Discord Bot",               ["DISCORD_BOT", "DISCORD_TOKEN"]),
    ("twilioApi",         "Twilio API",                ["TWILIO"]),
    ("whatsAppTriggerApi","WhatsApp Trigger",          ["WHATSAPP"]),

    # Productivity / knowledge
    ("airtableApi",             "Airtable API (legacy key)", ["AIRTABLE_KEY_LEGACY"]),
    ("airtableTokenApi",        "Airtable Personal Access Token", ["AIRTABLE"]),
    ("notionApi",               "Notion API",          ["NOTION"]),
    ("asanaApi",                "Asana API",           ["ASANA"]),
    ("clickUpApi",              "ClickUp API",         ["CLICKUP"]),
    ("linearApi",               "Linear API",          ["LINEAR"]),
    ("trelloApi",               "Trello API",          ["TRELLO"]),
    ("jiraSoftwareApi",         "Jira API",            ["JIRA"]),
    ("confluenceApi",           "Confluence API",      ["CONFLUENCE"]),
    ("mondayComApi",            "Monday.com API",      ["MONDAY"]),

    # Developer platforms
    ("githubApi",         "GitHub API",                ["GITHUB"]),
    ("gitlabApi",         "GitLab API",                ["GITLAB"]),
    ("bitbucketApi",      "Bitbucket API",             ["BITBUCKET"]),

    # Storage / cloud
    ("dropboxApi",        "Dropbox API",               ["DROPBOX"]),
    ("awsApi",            "AWS",                       ["AWS_ACCESS", "AWS_SECRET"]),
    ("s3",                "S3 (generic)",              ["S3_"]),

    # Payments / commerce
    ("stripeApi",         "Stripe API",                ["STRIPE"]),

    # Email
    ("sendGridApi",       "SendGrid API",              ["SENDGRID"]),
    ("mailchimpApi",      "Mailchimp API",             ["MAILCHIMP"]),
    ("sendInBlueApi",     "Brevo / Sendinblue API",    ["BREVO", "SENDINBLUE"]),
    ("postmarkApi",       "Postmark API",              ["POSTMARK"]),
    ("gmailOAuth2",       "Gmail OAuth2",              ["GMAIL_OAUTH"]),

    # Google APIs
    ("googleApi",         "Google API",                ["GOOGLE_API"]),
    ("googleSheetsOAuth2Api", "Google Sheets OAuth2",  ["GOOGLE_SHEETS"]),
    ("googleDriveOAuth2Api",  "Google Drive OAuth2",   ["GOOGLE_DRIVE"]),
    ("googleCalendarOAuth2Api","Google Calendar OAuth2", ["GOOGLE_CAL"]),

    # CRM / marketing
    ("hubspotApi",        "HubSpot API",               ["HUBSPOT"]),
    ("pipedriveApi",      "Pipedrive API",             ["PIPEDRIVE"]),
    ("zohoOAuth2Api",     "Zoho OAuth2",               ["ZOHO"]),
    ("zendeskApi",        "Zendesk API",               ["ZENDESK"]),
    ("intercomApi",       "Intercom API",              ["INTERCOM"]),

    # Databases / search / scraping
    ("mongoDb",           "MongoDB",                   ["MONGO"]),
    ("postgres",          "Postgres",                  ["POSTGRES", "PG_"]),
    ("mySql",             "MySQL",                     ["MYSQL"]),
    ("redis",             "Redis",                     ["REDIS"]),
    ("pineconeApi",       "Pinecone API",              ["PINECONE"]),
    ("qdrantApi",         "Qdrant API",                ["QDRANT"]),
    ("serpApi",           "SerpAPI",                   ["SERP"]),
    ("browserlessApi",    "Browserless API",           ["BROWSERLESS"]),

    # Generic
    ("httpHeaderAuth",    "HTTP Header Auth",          []),  # fallback, no auto-detect
    ("httpBasicAuth",     "HTTP Basic Auth",           []),
    ("httpBearerAuth",    "HTTP Bearer Auth",          []),
]


def detect_type_from_name(secret_name: str) -> str:
    """Return best-guess type for a secret name (substring match, case-insensitive)."""
    if not secret_name:
        return ""
    upper = secret_name.upper()
    for type_key, _, patterns in KNOWN_TYPES:
        for p in patterns:
            if p.upper() in upper:
                return type_key
    return ""


def display_name_for(type_key: str) -> str:
    """Return the display name for a known type, or the type key itself."""
    for t, display, _ in KNOWN_TYPES:
        if t == type_key:
            return display
    return type_key
