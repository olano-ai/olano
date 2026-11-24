// Where a human creates each provider's API key — the console/keys page we
// deep-link from key-entry UIs (onboarding wizard; reusable by ProvidersView).
// Keyed by the backend catalog's provider `key`. Providers with no static
// key page (ollama, litellm, openai-codex OAuth, vertex service accounts)
// are simply absent.
export const PROVIDER_KEY_URLS: Record<string, string> = {
  anthropic: "https://console.anthropic.com/settings/keys",
  openai: "https://platform.openai.com/api-keys",
  google_genai: "https://aistudio.google.com/apikey",
  groq: "https://console.groq.com/keys",
  openrouter: "https://openrouter.ai/settings/keys",
  deepseek: "https://platform.deepseek.com/api_keys",
  mistralai: "https://console.mistral.ai/api-keys",
  xai: "https://console.x.ai",
  cohere: "https://dashboard.cohere.com/api-keys",
  fireworks: "https://app.fireworks.ai/settings/users/api-keys",
  together: "https://api.together.ai/settings/api-keys",
  nvidia: "https://build.nvidia.com",
  perplexity: "https://www.perplexity.ai/settings/api",
  azure_openai: "https://portal.azure.com",
  bedrock: "https://console.aws.amazon.com/iam",
  vercel: "https://vercel.com/dashboard",
};
