from dataclasses import dataclass
from typing import Optional

@dataclass
class ModelPricing:
    input_per_1m: float       # USD per 1M input tokens
    output_per_1m: float      # USD per 1M output tokens
    context_window: int       # max context tokens
    max_output_tokens: int
    provider: str
    category: str             # chat | reasoning | code | embedding | search

@dataclass
class ProxyPricing:
    proxy_type: str
    starting_price_per_gb: float
    billing_model: str
    best_for: str
    provider: str

MODEL_PRICING: dict[str, ModelPricing] = {

    # ── OPENAI ────────────────────────────────────────────────────────────────
    "gpt-5":                  ModelPricing(5.00,   15.00, 400_000, 128_000, "OpenAI", "chat"),
    "gpt-5-mini":             ModelPricing(0.60,    2.40, 200_000,  64_000, "OpenAI", "chat"),
    "gpt-5-nano":             ModelPricing(0.10,    0.40, 128_000,  32_000, "OpenAI", "chat"),
    "gpt-5-nano-banana":      ModelPricing(0.12,    0.45, 128_000,  32_000, "OpenAI", "chat"),

    "gpt-4o":                 ModelPricing(2.50,   10.00, 128_000,  16_384, "OpenAI", "chat"),
    "gpt-4o-mini":            ModelPricing(0.15,    0.60, 128_000,  16_384, "OpenAI", "chat"),
    "gpt-4.1":                ModelPricing(2.00,    8.00, 1_047_576, 32_768, "OpenAI", "chat"),
    "gpt-4.1-mini":           ModelPricing(0.40,    1.60, 1_047_576, 32_768, "OpenAI", "chat"),
    "gpt-4o-2024-11-20":      ModelPricing(2.50,   10.00, 128_000,  16_384, "OpenAI", "chat"),
    "gpt-4-turbo":            ModelPricing(10.00,  30.00, 128_000,   4_096, "OpenAI", "chat"),
    "gpt-4":                  ModelPricing(30.00,  60.00,   8_192,   4_096, "OpenAI", "chat"),
    "gpt-3.5-turbo":          ModelPricing(0.50,    1.50,  16_385,   4_096, "OpenAI", "chat"),

    "o1":                     ModelPricing(15.00,  60.00, 200_000, 100_000, "OpenAI", "reasoning"),
    "o1-mini":                ModelPricing(1.10,    4.40, 128_000,  65_536, "OpenAI", "reasoning"),
    "o1-preview":             ModelPricing(15.00,  60.00, 128_000,  32_768, "OpenAI", "reasoning"),

    "o3":                     ModelPricing(10.00,  40.00, 200_000, 100_000, "OpenAI", "reasoning"),
    "o3-mini":                ModelPricing(1.10,    4.40, 200_000, 100_000, "OpenAI", "reasoning"),
    "o4-mini":                ModelPricing(1.10,    4.40, 200_000, 100_000, "OpenAI", "reasoning"),

    "text-embedding-3-small": ModelPricing(0.02,    0.00,   8_191,       0, "OpenAI", "embedding"),
    "text-embedding-3-large": ModelPricing(0.13,    0.00,   8_191,       0, "OpenAI", "embedding"),

    # ── ANTHROPIC ─────────────────────────────────────────────────────────────
    "claude-opus-4-5":            ModelPricing(15.00, 75.00, 200_000, 32_000, "Anthropic", "chat"),
    "claude-sonnet-4-5":          ModelPricing(3.00,  15.00, 200_000, 64_000, "Anthropic", "chat"),
    "claude-haiku-4-5":           ModelPricing(0.80,   4.00, 200_000,  8_096, "Anthropic", "chat"),
    "claude-3-5-sonnet-20241022": ModelPricing(3.00,  15.00, 200_000,  8_096, "Anthropic", "chat"),
    "claude-3-5-haiku-20241022":  ModelPricing(0.80,   4.00, 200_000,  8_096, "Anthropic", "chat"),
    "claude-3-opus-20240229":     ModelPricing(15.00, 75.00, 200_000,  4_096, "Anthropic", "chat"),

    # ── GOOGLE GEMINI ─────────────────────────────────────────────────────────
    "gemini-2.5-pro":         ModelPricing(1.25,  10.00, 1_048_576, 65_536, "Google", "chat"),
    "gemini-2.5-flash":       ModelPricing(0.15,   0.60, 1_048_576, 65_536, "Google", "chat"),
    "gemini-2.0-flash":       ModelPricing(0.10,   0.40, 1_048_576,  8_192, "Google", "chat"),
    "gemini-1.5-pro":         ModelPricing(1.25,   5.00, 2_097_152,  8_192, "Google", "chat"),
    "gemini-1.5-flash":       ModelPricing(0.075,  0.30, 1_048_576,  8_192, "Google", "chat"),

    "text-embedding-004":     ModelPricing(0.025,  0.00,     2_048,      0, "Google", "embedding"),

    # ── META LLAMA ────────────────────────────────────────────────────────────
    "llama-3.3-70b-versatile": ModelPricing(0.59, 0.79, 128_000, 32_768, "Meta/Groq",     "chat"),
    "llama-3.1-405b-instruct": ModelPricing(3.00, 3.00, 131_072, 16_384, "Meta/Together", "chat"),
    "llama-3.1-70b-instruct":  ModelPricing(0.88, 0.88, 131_072, 16_384, "Meta/Together", "chat"),
    "llama-3.1-8b-instruct":   ModelPricing(0.18, 0.18, 131_072, 16_384, "Meta/Together", "chat"),

    # ── MISTRAL ───────────────────────────────────────────────────────────────
    "mistral-large-2411":     ModelPricing(2.00, 6.00, 128_000, 16_384, "Mistral", "chat"),
    "mistral-small-2503":     ModelPricing(0.10, 0.30,  32_000,  8_192, "Mistral", "chat"),
    "codestral-2501":         ModelPricing(0.30, 0.90, 256_000, 16_384, "Mistral", "code"),

    # ── COHERE ────────────────────────────────────────────────────────────────
    "command-r-plus-08-2024": ModelPricing(2.50, 10.00, 128_000, 4_096, "Cohere", "chat"),
    "command-r-08-2024":      ModelPricing(0.15,  0.60, 128_000, 4_096, "Cohere", "chat"),
    "embed-english-v3.0":     ModelPricing(0.10,  0.00,     512,     0, "Cohere", "embedding"),

    # ── GROQ ──────────────────────────────────────────────────────────────────
    "mixtral-8x7b-32768":     ModelPricing(0.27, 0.27, 32_768, 32_768, "Groq",        "chat"),
    "gemma2-9b-it":           ModelPricing(0.20, 0.20,  8_192,  8_192, "Groq/Google", "chat"),

    # ── DEEPSEEK ──────────────────────────────────────────────────────────────
    "deepseek-chat":          ModelPricing(0.27, 1.10, 64_000, 8_192, "DeepSeek", "chat"),
    "deepseek-reasoner":      ModelPricing(0.55, 2.19, 64_000, 8_192, "DeepSeek", "reasoning"),

    # ── PERPLEXITY ────────────────────────────────────────────────────────────
    "sonar-pro":              ModelPricing(3.00, 15.00, 200_000, 8_000, "Perplexity", "search"),
    "sonar":                  ModelPricing(1.00,  1.00, 127_072, 8_000, "Perplexity", "search"),

    # ── xAI GROK ──────────────────────────────────────────────────────────────
    "grok-3":                 ModelPricing(3.00,  15.00, 131_072, 131_072, "xAI", "chat"),
    "grok-3-mini":            ModelPricing(0.30,   0.50, 131_072, 131_072, "xAI", "reasoning"),
}


# ── Model name aliases ────────────────────────────────────────────────────────
# Maps shorthand / deployment names → canonical MODEL_PRICING keys.
# Kept here so every backend endpoint benefits automatically.
MODEL_ALIASES: dict[str, str] = {
    # Groq hosted Llama variants
    "groq-llama":              "llama-3.3-70b-versatile",
    "groq-llama3":             "llama-3.3-70b-versatile",
    "llama3":                  "llama-3.3-70b-versatile",
    "llama-3":                 "llama-3.3-70b-versatile",
    "llama3-70b":              "llama-3.3-70b-versatile",
    "llama-3-70b":             "llama-3.3-70b-versatile",
    "llama-3.3-70b":           "llama-3.3-70b-versatile",
    "llama-3.1-70b":           "llama-3.1-70b-instruct",
    "llama-3.1-8b":            "llama-3.1-8b-instruct",
    "llama-3.1-405b":          "llama-3.1-405b-instruct",
    # Azure deployment name aliases → canonical OpenAI names
    "gpt4o":                   "gpt-4o",
    "gpt-4o-latest":           "gpt-4o",
    "gpt-4o-deployment":       "gpt-4o",
    "azure-gpt":               "gpt-4o",
    "azure-gpt4o":             "gpt-4o",
    "azure-gpt-4o":            "gpt-4o",
    "gpt4":                    "gpt-4",
    "gpt-4-32k":               "gpt-4",
    "gpt-35-turbo":            "gpt-3.5-turbo",
    "gpt-35-turbo-16k":        "gpt-3.5-turbo",
    "gpt35":                   "gpt-3.5-turbo",
    # Claude shorthand
    "claude-3-5-sonnet":       "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku":        "claude-3-5-haiku-20241022",
    "claude-3-haiku":          "claude-3-5-haiku-20241022",
    "claude-3-opus":           "claude-3-opus-20240229",
    "claude-sonnet":           "claude-3-5-sonnet-20241022",
    "claude-haiku":            "claude-3-5-haiku-20241022",
    "claude-opus":             "claude-3-opus-20240229",
    "claude-sonnet-4":         "claude-sonnet-4-5",
    "claude-haiku-4":          "claude-haiku-4-5",
    "claude-opus-4":           "claude-opus-4-5",
    # Gemini shorthand
    "gemini-pro":              "gemini-1.5-pro",
    "gemini-flash":            "gemini-2.0-flash",
    "gemini-2.5":              "gemini-2.5-pro",
    "gemini-2.0":              "gemini-2.0-flash",
    "gemini-1.5":              "gemini-1.5-pro",
    # Mistral shorthand
    "mistral-large":           "mistral-large-2411",
    "mistral-small":           "mistral-small-2503",
    "codestral":               "codestral-2501",
}


# ── Provider-specific pricing overrides ──────────────────────────────────────
# Same model may be priced differently per provider (e.g. Azure vs OpenAI).
# Key: (provider_lowercase, canonical_model_name)
# Lookup order: PROVIDER_PRICING → MODEL_PRICING → DB → default rate
PROVIDER_PRICING: dict[tuple[str, str], ModelPricing] = {
    # Azure OpenAI — input is half of OpenAI direct for GPT-5-nano
    ("azure_openai", "gpt-5-nano"):   ModelPricing(0.05,   0.40, 128_000, 32_000, "Azure OpenAI", "chat"),
    ("azure_openai", "gpt-5-mini"):   ModelPricing(0.55,   2.20, 200_000, 64_000, "Azure OpenAI", "chat"),
    ("azure_openai", "gpt-5"):        ModelPricing(4.50,  13.50, 400_000,128_000, "Azure OpenAI", "chat"),
    ("azure_openai", "gpt-4o"):       ModelPricing(2.50,  10.00, 128_000, 16_384, "Azure OpenAI", "chat"),
    ("azure_openai", "gpt-4o-mini"):  ModelPricing(0.15,   0.60, 128_000, 16_384, "Azure OpenAI", "chat"),
    ("azure_openai", "gpt-4.1"):      ModelPricing(2.00,   8.00, 1_047_576, 32_768, "Azure OpenAI", "chat"),
    ("azure_openai", "gpt-4.1-mini"): ModelPricing(0.40,   1.60, 1_047_576, 32_768, "Azure OpenAI", "chat"),
    ("azure_openai", "gpt-4-turbo"):  ModelPricing(10.00, 30.00, 128_000,  4_096, "Azure OpenAI", "chat"),
    ("azure_openai", "gpt-4"):        ModelPricing(30.00, 60.00,   8_192,  4_096, "Azure OpenAI", "chat"),
    ("azure_openai", "gpt-3.5-turbo"):  ModelPricing(0.50,   1.50,  16_385,  4_096, "Azure OpenAI", "chat"),
    # Azure OpenAI embedding models
    ("azure_openai", "text-embedding-3-small"): ModelPricing(0.02, 0.00, 8_191, 0, "Azure OpenAI", "embedding"),
    ("azure_openai", "text-embedding-3-large"): ModelPricing(0.13, 0.00, 8_191, 0, "Azure OpenAI", "embedding"),
    ("azure_openai", "text-embedding-ada-002"): ModelPricing(0.10, 0.00, 8_191, 0, "Azure OpenAI", "embedding"),
    # Add more provider-specific overrides here as needed
}


def normalize_provider(provider: str) -> str:
    """Normalise provider string to a consistent lowercase key."""
    return (provider or "").lower().replace("-", "_").replace(" ", "_")


def get_model_pricing_for_provider(model_name: str, provider: str) -> Optional[ModelPricing]:
    """Return provider-specific pricing if available, otherwise fall back to generic catalog."""
    canonical = normalize_model_name(model_name or "")
    prov = normalize_provider(provider)
    return PROVIDER_PRICING.get((prov, canonical)) or MODEL_PRICING.get(canonical)


def normalize_model_name(model_name: str) -> str:
    """Resolve aliases and casing variations to a canonical MODEL_PRICING key."""
    if not model_name:
        return model_name
    if model_name in MODEL_PRICING:
        return model_name
    if model_name in MODEL_ALIASES:
        return MODEL_ALIASES[model_name]
    lower = model_name.lower()
    if lower in MODEL_PRICING:
        return lower
    if lower in MODEL_ALIASES:
        return MODEL_ALIASES[lower]
    return model_name


def get_model_pricing(model_name: str) -> Optional[ModelPricing]:
    """Return ModelPricing for model_name (resolves aliases), or None if unknown."""
    return MODEL_PRICING.get(normalize_model_name(model_name))


def calculate_token_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
) -> dict:
    """
    Dynamically compute input_token_cost, output_token_cost, and total_cost
    from the MODEL_PRICING catalogue.

    Rates are USD per 1 million tokens (input_per_1m / output_per_1m).
    Resolves model aliases before lookup.
    Returns zeroed costs for unknown models — never falls back to static values.
    """
    canonical = normalize_model_name(model_name or "")
    pricing = MODEL_PRICING.get(canonical)
    if pricing and (input_tokens > 0 or output_tokens > 0):
        input_cost = round((input_tokens / 1_000_000) * pricing.input_per_1m, 6)
        output_cost = round((output_tokens / 1_000_000) * pricing.output_per_1m, 6)
    else:
        input_cost = 0.0
        output_cost = 0.0
    return {
        "input_token_cost": input_cost,
        "output_token_cost": output_cost,
        "total_cost": round(input_cost + output_cost, 6),
    }


# ── PROXY PRICING ─────────────────────────────────────────────────────────────
PROXY_PRICING: dict[str, ProxyPricing] = {

    # ── OXYLABS ───────────────────────────────────────────────────────────────
    "oxylabs-residential": ProxyPricing(
        proxy_type="Residential Proxies",
        starting_price_per_gb=8.0,
        billing_model="Traffic usage",
        best_for="Web scraping, automation",
        provider="Oxylabs"
    ),

    "oxylabs-datacenter": ProxyPricing(
        proxy_type="Datacenter Proxies",
        starting_price_per_gb=1.2,
        billing_model="Traffic usage",
        best_for="High-speed scraping",
        provider="Oxylabs"
    ),

    "oxylabs-mobile": ProxyPricing(
        proxy_type="Mobile Proxies",
        starting_price_per_gb=9.0,
        billing_model="Traffic usage",
        best_for="Mobile app scraping",
        provider="Oxylabs"
    ),

    # ── SMARTPROXY ────────────────────────────────────────────────────────────
    "smartproxy-residential": ProxyPricing(
        proxy_type="Residential Proxies",
        starting_price_per_gb=7.5,
        billing_model="Traffic usage",
        best_for="Automation, bypass detection",
        provider="Smartproxy"
    ),

    # ── BRIGHT DATA ───────────────────────────────────────────────────────────
    "brightdata-residential": ProxyPricing(
        proxy_type="Residential Proxies",
        starting_price_per_gb=8.4,
        billing_model="Traffic usage",
        best_for="Enterprise scraping",
        provider="Bright Data"
    ),
}